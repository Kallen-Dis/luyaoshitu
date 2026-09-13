"""百度地图 Web 服务 API 异步客户端。

设计要点全部来自 reports/quota-report.md 的实测结论：

- **共享连接池**：进程级单例 AsyncClient，长 keepalive。实测复用连接比每次新建
  快约一倍（0.92s vs 1.88s），省下的正是一次 TLS 握手。
- **令牌桶限流**：全局出口限速，所有接口共用一个桶。实测地理编码 12 并发安全、
  16 触发限流，默认取 8 留余量。
- **错误码分流**：401/402 退避重试；301/302 抛 QuotaExhaustedError 立即终止；
  200/210/220 抛 ConfigurationError。三类处置方式完全不同，混用代价很大。
- **磁盘缓存**：按坐标网格量化，地点检索的日配额靠它省下来。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Iterable, Sequence

import httpx

from ..config import Settings, get_settings
from .cache import DiskCache
from .errors import (
    RETRYABLE_STATUS,
    BaiduApiError,
    ConfigurationError,
    QuotaExhaustedError,
    classify,
)

BASE_URL = "https://api.map.baidu.com"


class TokenBucket:
    """最简令牌桶：把出口速率钳在 qps 以内。

    只记录"下一个可发令牌的时刻"，比维护令牌计数更简单，且天然平滑——
    不会出现桶攒满后瞬间放出一大批请求、恰好撞上百度并发限制的情况。
    """

    def __init__(self, qps: float) -> None:
        self._interval = 1.0 / max(qps, 0.1)
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_at - now)
            self._next_at = max(now, self._next_at) + self._interval
        if wait:
            await asyncio.sleep(wait)


class BaiduMapClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self._s = settings or get_settings()
        self._ak = self._s.require_server_ak()
        self._bucket = TokenBucket(self._s.max_qps)
        self._cache = DiskCache(self._s.cache_dir, self._s.cache_grid_m)
        self._client: httpx.AsyncClient | None = None
        # 配额一旦耗尽，同一进程内不再重复试探该接口，避免每次调用都白等一轮重试
        self._exhausted: set[str] = set()

    async def __aenter__(self) -> "BaiduMapClient":
        limits = httpx.Limits(
            max_connections=max(self._s.max_qps * 2, 16),
            max_keepalive_connections=max(self._s.max_qps * 2, 16),
            keepalive_expiry=120.0,
        )
        self._client = httpx.AsyncClient(timeout=self._s.http_timeout, limits=limits)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------- 底层请求 ----------

    async def _request(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("BaiduMapClient 必须在 async with 块中使用")
        if endpoint in self._exhausted:
            raise QuotaExhaustedError(302, "本进程内该接口已确认配额耗尽", endpoint)

        payload = {**params, "ak": self._ak}
        last_error: Exception | None = None

        for attempt in range(self._s.max_retries + 1):
            await self._bucket.acquire()
            try:
                resp = await self._client.get(BASE_URL + endpoint, params=payload)
                body = resp.json()
                status = int(body.get("status", -1))
                if status == 0:
                    return body

                error = classify(status, str(body.get("message", "")), endpoint)
                if isinstance(error, QuotaExhaustedError):
                    self._exhausted.add(endpoint)
                    raise error
                if isinstance(error, ConfigurationError):
                    raise error
                if status in RETRYABLE_STATUS and attempt < self._s.max_retries:
                    last_error = error
                    await asyncio.sleep(self._s.retry_backoff * (2**attempt))
                    continue
                raise error
            except (QuotaExhaustedError, ConfigurationError):
                raise
            except (httpx.HTTPError, ValueError) as exc:
                # 网络抖动与 JSON 解析失败同属瞬时故障。实测本机走系统代理，
                # 偶发 ConnectError 属常态，必须重试而非上抛。
                last_error = exc
                if attempt < self._s.max_retries:
                    await asyncio.sleep(self._s.retry_backoff * (2**attempt))
                    continue
                raise

        raise last_error or BaiduApiError(-1, "重试耗尽", endpoint)

    # ---------- 业务接口 ----------

    async def geocode(self, address: str) -> tuple[float, float]:
        """地址转 BD09 坐标。"""
        body = await self._request("/geocoding/v3/", {"address": address, "output": "json"})
        loc = body["result"]["location"]
        return float(loc["lat"]), float(loc["lng"])

    async def search_poi(
        self, keyword: str, lat: float, lng: float, radius: int = 1000, page_size: int = 20
    ) -> list[dict[str, Any]] | None:
        """周边检索。返回 None 表示查询失败或配额耗尽——调用方必须区别对待，
        绝不能当成「该区域没有此类设施」，否则 API 故障会被伪装成服务盲区。"""
        cache_path = self._cache.key_for_point("poi", lat, lng, keyword, radius)
        cached = self._cache.read(cache_path)
        if cached is not None:
            return cached["results"]

        try:
            body = await self._request(
                "/place/v2/search",
                {
                    "query": keyword,
                    "location": f"{lat:.6f},{lng:.6f}",
                    "radius": radius,
                    "output": "json",
                    "page_size": page_size,
                },
            )
        except (QuotaExhaustedError, BaiduApiError):
            return None

        results = body.get("results", [])
        self._cache.write(cache_path, {"keyword": keyword, "total": body.get("total"), "results": results})
        return results

    async def walking_matrix(
        self, origin: tuple[float, float], destinations: Sequence[tuple[float, float]]
    ) -> list[dict[str, float] | None]:
        """批量步行算路：一个起点到多个终点的真实路网距离与耗时。

        自动按 matrix_batch_size（实测硬上限 100）分批，各批并发提交，
        速率由令牌桶统一钳制。返回值与 destinations 一一对应，
        某项为 None 表示该点不可达或查询失败。
        """
        if not destinations:
            return []

        size = self._s.matrix_batch_size
        batches = [destinations[i : i + size] for i in range(0, len(destinations), size)]
        chunks = await asyncio.gather(
            *(self._walking_matrix_batch(origin, b) for b in batches),
            return_exceptions=True,
        )

        out: list[dict[str, float] | None] = []
        for batch, chunk in zip(batches, chunks):
            if isinstance(chunk, BaseException):
                out.extend([None] * len(batch))  # 整批失败，如实标记缺失
            else:
                out.extend(chunk)
        return out

    async def _walking_matrix_batch(
        self, origin: tuple[float, float], destinations: Sequence[tuple[float, float]]
    ) -> list[dict[str, float] | None]:
        # 先查缓存，只把未命中的点发出去。等时圈采样在相邻半径上高度重复，
        # 命中率通常很可观。
        results: list[dict[str, float] | None] = [None] * len(destinations)
        misses: list[int] = []
        for i, dest in enumerate(destinations):
            path = self._cache.key_for_pair("walk", origin[0], origin[1], dest[0], dest[1])
            cached = self._cache.read(path)
            if cached is not None:
                results[i] = cached
            else:
                misses.append(i)

        if not misses:
            return results

        dest_param = "|".join(
            f"{destinations[i][0]:.6f},{destinations[i][1]:.6f}" for i in misses
        )
        body = await self._request(
            "/routematrix/v2/walking",
            {
                "origins": f"{origin[0]:.6f},{origin[1]:.6f}",
                "destinations": dest_param,
                "output": "json",
            },
        )

        for slot, item in zip(misses, body.get("result", [])):
            distance = item.get("distance", {}).get("value")
            duration = item.get("duration", {}).get("value")
            if distance is None or duration is None:
                continue
            entry = {"distance_m": float(distance), "duration_s": float(duration)}
            results[slot] = entry
            dest = destinations[slot]
            self._cache.write(
                self._cache.key_for_pair("walk", origin[0], origin[1], dest[0], dest[1]), entry
            )
        return results
