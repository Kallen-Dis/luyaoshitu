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
from collections.abc import Sequence
from typing import Any, Self

import httpx

from ..config import Settings, get_settings
from ..travel import TravelMode, get_mode
from .cache import DiskCache
from .errors import (
    RETRYABLE_STATUS,
    BaiduApiError,
    ConfigurationError,
    QuotaExhaustedError,
    classify,
)

BASE_URL = "https://api.map.baidu.com"

# 多少个点对折算一个令牌。一次 100 点对的批量算路在服务端的工作量远大于
# 一次 4 点对的请求，按请求数计的限速对它并不公平。并发配额的确切计量口径
# 官方未公开，这里按点对数折算取一个保守值：8 QPS 下 100 点对约占 0.6 秒。
PAIRS_PER_TOKEN = 20.0

# 同时在途的批量算路请求数上限，取 1 即完全串行。
#
# 串行是最省配额的做法。并发放行时，一旦撞上限流或配额耗尽，多个分块会各自
# 展开退避重试并互相踩踏，既烧配额又拿不到结果；串行后每次重试都有干净的窗口，
# 且配额耗尽能在第一块就被 _exhausted 拦住，不会再白发十几个必然失败的请求。
MAX_INFLIGHT_MATRIX = 1


def _endpoint_label(endpoint: str) -> str:
    """把接口路径归并为短名，供配额统计按接口类别展示。"""
    if "/place/v2/search" in endpoint:
        return "poi"
    if "geocoding" in endpoint:
        return "geocode"
    if "geoconv" in endpoint:
        return "geoconv"
    if "routematrix" in endpoint:
        return "matrix"
    if "directionlite" in endpoint:
        return "route"
    return endpoint.strip("/").rsplit("/", 1)[-1]


class TokenBucket:
    """最简令牌桶：把出口速率钳在 qps 以内。

    只记录"下一个可发令牌的时刻"，比维护令牌计数更简单，且天然平滑——
    不会出现桶攒满后瞬间放出一大批请求、恰好撞上百度并发限制的情况。

    cost 让一次请求可以占用多个令牌：批量算路按点对数付出服务端算力，
    100 个点对的矩阵与 1 个点对的查询不该占用同样的速率预算。
    """

    def __init__(self, qps: float) -> None:
        self._interval = 1.0 / max(qps, 0.1)
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def acquire(self, cost: float = 1.0) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next_at - now)
            self._next_at = max(now, self._next_at) + self._interval * max(cost, 1.0)
        if wait:
            await asyncio.sleep(wait)


class BaiduMapClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self._s = settings or get_settings()
        self._ak = self._s.require_server_ak()
        self._bucket = TokenBucket(self._s.max_qps)
        self._cache = DiskCache(self._s.cache_dir, self._s.cache_grid_m)
        self._client: httpx.AsyncClient | None = None
        self._matrix_gate = asyncio.Semaphore(MAX_INFLIGHT_MATRIX)
        # 整块失败的矩阵请求。失败会让下游把网格记成「测距失败」，
        # 与真实的不可达长得一模一样，不留痕就只能靠猜。
        self.matrix_failures: list[str] = []
        # 实发的算路点对数。批量算路的**日配额按点对数计量**，不按请求数：
        # 实测一天发出约 140 次请求、共 2479 个点对即触发 302，而同日的地点检索
        # 只用掉 38 次（额度 3000）。省配额要盯的是这个数，不是请求数。
        self.matrix_pairs = 0
        # 按接口归类的实发 HTTP 请求数。与 matrix_pairs 一起构成配额消耗证据链：
        # 前端展示「本次消耗多少」靠它与调用前的快照做差（客户端是进程级共享的）。
        self.request_counts: dict[str, int] = {}
        # 配额一旦耗尽，同一进程内不再重复试探该接口，避免每次调用都白等一轮重试
        self._exhausted: set[str] = set()

    @property
    def failed_matrix_blocks(self) -> int:
        return len(self.matrix_failures)

    def usage_snapshot(self) -> dict[str, Any]:
        """配额消耗快照，供单次计算在调用前后做差，得出「本次」实发量。

        客户端是进程级单例，matrix_pairs 与 request_counts 都是累计值；
        不取差值就会把一天的总消耗当成单次计算的结果。
        """
        return {
            "matrix_pairs": self.matrix_pairs,
            "matrix_failures": len(self.matrix_failures),
            "requests": dict(self.request_counts),
        }

    async def __aenter__(self) -> Self:
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

    async def _request(
        self, endpoint: str, params: dict[str, Any], cost: float = 1.0
    ) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("BaiduMapClient 必须在 async with 块中使用")
        if endpoint in self._exhausted:
            raise QuotaExhaustedError(302, "本进程内该接口已确认配额耗尽", endpoint)

        payload = {**params, "ak": self._ak}
        last_error: Exception | None = None

        for attempt in range(self._s.max_retries + 1):
            await self._bucket.acquire(cost)
            try:
                resp = await self._client.get(BASE_URL + endpoint, params=payload)
                # 每次实际发出的 HTTP 请求都计数（含失败后的重试），
                # 这是配额消耗证据链里「请求数」一侧的原始数据。
                label = _endpoint_label(endpoint)
                self.request_counts[label] = self.request_counts.get(label, 0) + 1
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

    async def geoconv(
        self, coords: Sequence[tuple[float, float]], from_sys: str = "wgs84"
    ) -> list[tuple[float, float]]:
        """坐标转换到 BD09。from_sys 取值 wgs84 / gcj02 / bd09。

        支持 WGS84(GPS) 与 GCJ02(高德/腾讯) 输入是本项目的入口能力：
        命题只说「输入中心点坐标」，不限定坐标系，收窄到 BD09 会把一
        大批用户挡在门外。geoconv 接口无日配额限制，转换开销可忽略。
        """
        codes = {"wgs84": "1", "gcj02": "3", "bd09": "5"}
        if from_sys not in codes:
            raise ValueError(f"未知坐标系：{from_sys}")
        if not coords:
            return []
        coord_str = ";".join(f"{lng:.6f},{lat:.6f}" for lat, lng in coords)
        body = await self._request(
            "/geoconv/v1/",
            {"coords": coord_str, "from": codes[from_sys], "to": "5", "output": "json"},
        )
        out: list[tuple[float, float]] = []
        for item in body.get("result", []):
            out.append((float(item["y"]), float(item["x"])))  # 内部保持 (lat, lng)
        return out

    async def search_poi(
        self,
        keyword: str,
        lat: float,
        lng: float,
        radius: int = 1000,
        page_size: int = 20,
        page_num: int = 0,
    ) -> list[dict[str, Any]] | None:
        """周边检索单页。返回 None 表示查询失败或配额耗尽——调用方必须区别对待，
        绝不能当成「该区域没有此类设施」，否则 API 故障会被伪装成服务盲区。"""
        cache_path = self._cache.key_for_point("poi", lat, lng, keyword, radius, page_num)
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
                    "page_num": page_num,
                },
            )
        except (QuotaExhaustedError, BaiduApiError):
            return None

        results = body.get("results", [])
        self._cache.write(
            cache_path,
            {"keyword": keyword, "total": body.get("total"), "results": results},
        )
        return results

    async def search_poi_all(
        self,
        keyword: str,
        lat: float,
        lng: float,
        radius: int = 1000,
        page_size: int = 20,
        max_pages: int = 3,
    ) -> list[dict[str, Any]] | None:
        """翻页取全量周边检索结果。

        分页必须串行：只有拿到当前页才知道是否还有下一页，盲目并发预取整页区间
        会在设施稀少的品类上白烧配额——而地点检索正是本项目最紧的那项。

        任何一页失败即返回 None。半截结果比没有结果更危险：它会让某个品类看起来
        数量偏少甚至为零，从而把 API 故障伪装成服务盲区。
        """
        collected: list[dict[str, Any]] = []
        for page in range(max_pages):
            page_results = await self.search_poi(
                keyword, lat, lng, radius, page_size=page_size, page_num=page
            )
            if page_results is None:
                return None
            collected.extend(page_results)
            if len(page_results) < page_size:
                break  # 不满一页说明已到末页
        return collected

    async def walking_matrix(
        self, origin: tuple[float, float], destinations: Sequence[tuple[float, float]]
    ) -> list[dict[str, float] | None]:
        """步行批量算路。保留此名以免测试与调用方大面积改动。"""
        return await self.route_matrix("walk", origin, destinations)

    async def walking_matrix_grid(
        self,
        origins: Sequence[tuple[float, float]],
        destinations: Sequence[tuple[float, float]],
    ) -> list[list[dict[str, float] | None]]:
        return await self.route_matrix_grid("walk", origins, destinations)

    async def route_matrix(
        self,
        mode_id: str,
        origin: tuple[float, float],
        destinations: Sequence[tuple[float, float]],
    ) -> list[dict[str, float] | None]:
        """一个起点到多个终点的真实路网距离与耗时。出行方式决定路网与是否计入路况。"""
        if not destinations:
            return []
        mode = get_mode(mode_id)
        size = min(self._s.matrix_batch_size, mode.matrix_product_limit)
        batches = [destinations[i : i + size] for i in range(0, len(destinations), size)]
        chunks = await asyncio.gather(
            *(self._route_matrix_batch(mode, origin, b) for b in batches),
            return_exceptions=True,
        )

        out: list[dict[str, float] | None] = []
        for batch, chunk in zip(batches, chunks, strict=True):
            if isinstance(chunk, BaseException):
                out.extend([None] * len(batch))
            else:
                out.extend(chunk)
        return out

    async def route_matrix_grid(
        self,
        mode_id: str,
        origins: Sequence[tuple[float, float]],
        destinations: Sequence[tuple[float, float]],
    ) -> list[list[dict[str, float] | None]]:
        """多起点 × 多终点矩阵。乘积上限随出行方式变化（骑行文档为 50）。"""
        if not origins or not destinations:
            return [[None] * len(destinations) for _ in origins]

        mode = get_mode(mode_id)
        budget = max(1, min(self._s.matrix_batch_size, mode.matrix_product_limit))
        d_size = min(len(destinations), budget)
        o_size = max(1, budget // d_size)

        out: list[list[dict[str, float] | None]] = [
            [None] * len(destinations) for _ in origins
        ]
        tasks = []
        for oi in range(0, len(origins), o_size):
            for di in range(0, len(destinations), d_size):
                tasks.append((oi, di))

        async def run(oi: int, di: int) -> None:
            o_chunk = origins[oi : oi + o_size]
            d_chunk = destinations[di : di + d_size]
            block = await self._matrix_block(mode, o_chunk, d_chunk)
            for i, row in enumerate(block):
                for j, cell in enumerate(row):
                    out[oi + i][di + j] = cell

        await asyncio.gather(*(run(oi, di) for oi, di in tasks))
        return out

    async def _matrix_block(
        self,
        mode: TravelMode,
        origins: Sequence[tuple[float, float]],
        destinations: Sequence[tuple[float, float]],
    ) -> list[list[dict[str, float] | None]]:
        """请求一个矩形块。缓存命名空间含出行方式，步行旧缓存仍可复用。"""
        cached_block: list[list[dict[str, float] | None]] = []
        complete = True
        for o in origins:
            row: list[dict[str, float] | None] = []
            for d in destinations:
                entry = self._cache.read(
                    self._cache.key_for_pair(mode.cache_ns, o[0], o[1], d[0], d[1])
                )
                if entry is None:
                    complete = False
                row.append(entry)
            cached_block.append(row)
        if complete:
            return cached_block

        self.matrix_pairs += len(origins) * len(destinations)
        params: dict[str, Any] = {
            "origins": "|".join(f"{o[0]:.6f},{o[1]:.6f}" for o in origins),
            "destinations": "|".join(f"{d[0]:.6f},{d[1]:.6f}" for d in destinations),
            "output": "json",
            **{k: v for k, v in mode.extra_params.items()},
        }
        try:
            async with self._matrix_gate:
                body = await self._request(
                    mode.endpoint,
                    params,
                    cost=len(origins) * len(destinations) / PAIRS_PER_TOKEN,
                )
        except BaiduApiError as exc:
            self.matrix_failures.append(
                f"{mode.id} {len(origins)}x{len(destinations)} status={exc.status} {exc}"
            )
            return cached_block

        flat = body.get("result", [])
        for i, o in enumerate(origins):
            for j, d in enumerate(destinations):
                idx = i * len(destinations) + j
                if idx >= len(flat):
                    continue
                item = flat[idx]
                distance = item.get("distance", {}).get("value")
                duration = item.get("duration", {}).get("value")
                if distance is None or duration is None:
                    continue
                entry = {"distance_m": float(distance), "duration_s": float(duration)}
                cached_block[i][j] = entry
                self._cache.write(
                    self._cache.key_for_pair(mode.cache_ns, o[0], o[1], d[0], d[1]),
                    entry,
                )
        return cached_block

    async def _route_matrix_batch(
        self,
        mode: TravelMode,
        origin: tuple[float, float],
        destinations: Sequence[tuple[float, float]],
    ) -> list[dict[str, float] | None]:
        results: list[dict[str, float] | None] = [None] * len(destinations)
        misses: list[int] = []
        for i, dest in enumerate(destinations):
            path = self._cache.key_for_pair(
                mode.cache_ns, origin[0], origin[1], dest[0], dest[1]
            )
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
        self.matrix_pairs += len(misses)
        params: dict[str, Any] = {
            "origins": f"{origin[0]:.6f},{origin[1]:.6f}",
            "destinations": dest_param,
            "output": "json",
            **{k: v for k, v in mode.extra_params.items()},
        }
        async with self._matrix_gate:
            body = await self._request(
                mode.endpoint,
                params,
                cost=len(misses) / PAIRS_PER_TOKEN,
            )

        for slot, item in zip(misses, body.get("result", []), strict=False):
            distance = item.get("distance", {}).get("value")
            duration = item.get("duration", {}).get("value")
            if distance is None or duration is None:
                continue
            entry = {"distance_m": float(distance), "duration_s": float(duration)}
            results[slot] = entry
            dest = destinations[slot]
            self._cache.write(
                self._cache.key_for_pair(
                    mode.cache_ns, origin[0], origin[1], dest[0], dest[1]
                ),
                entry,
            )
        return results

