"""百度地图 Web 服务 API 异步客户端。

设计要点都来自实测：接口连通、单次往返与并发上限见 docs/api-optimization.md 第 4.1 节，
连接复用见 scripts/probe_network.py。各项做法省多少，
用真实缓存回放做了对照实验（scripts/benchmark_api.py → reports/api-benchmark.md）。

- **共享连接池**：进程级单例 AsyncClient，长 keepalive。复用连接省掉每次的 TLS 握手：
  两次链路诊断分别测得新建 1.88 秒对复用 0.92 秒、新建 2.5 秒对复用 0.54 秒（随时段波动），
  而且新建连接偶发 ConnectError。
- **令牌桶限流**：全局出口限速，所有接口共用一个桶。实测地理编码 12 并发安全、
  16 触发限流，默认取 8 留余量。
- **错误码分流**：401/402 退避重试；301/302 抛 QuotaExhaustedError 立即终止；
  200/210/220 抛 ConfigurationError。三类处置方式完全不同，混用代价很大。
- **磁盘缓存**：按坐标网格量化，地点检索的日配额靠它省下来。
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Self

import httpx

from ..config import Settings, get_settings
from ..isochrone.geometry import decode_baidu_path
from ..travel import TravelMode, get_mode
from .cache import METERS_PER_DEGREE, DiskCache
from .errors import (
    RETRYABLE_STATUS,
    BaiduApiError,
    ConfigurationError,
    QuotaExhaustedError,
    classify,
    service_label,
)

BASE_URL = "https://api.map.baidu.com"

# 多少个点对折算一个令牌。一次 100 点对的批量算路在服务端的工作量远大于
# 一次 4 点对的请求，按请求数计的限速对它并不公平。并发配额的确切计量口径
# 官方未公开，这里按点对数折算取一个保守值：8 QPS 下 100 点对约占 0.6 秒。
PAIRS_PER_TOKEN = 20.0

# 同时在途的批量算路请求数上限见 Settings.matrix_concurrency（默认 3）。
#
# 完全串行最保守，但一次网格盲区判定要发几十个小请求，每个都要等约 0.4 秒往返，
# 串行时大部分时间耗在等网络上，令牌桶的 8 QPS 远没用满。回放实验（曹杨圈内 100 米网格）：
# 在途 1 / 2 / 3 / 4 / 6 → 28.3 / 17.4 / 14.1 / 12.8 / 12.3 秒，3 以后收益很小。
# 放开到 3 个在途，吞吐大致追平令牌桶上限，而并发的两个老风险分别兜住：
# - 配额耗尽：第一个拿到 302 的请求立即熔断该接口，其余在途最多再浪费 2 个；
# - 限流重试：退避时间加随机抖动，避免 3 个请求同时醒来再次撞上 401。
# 改回 1 即恢复完全串行（环境变量 BAIDU_MATRIX_CONCURRENCY=1）。

# 路线步骤说明里夹着 <b>…</b>，入缓存前剥掉，界面与判定都只用纯文本
_TAG_RE = re.compile(r"<[^>]+>")

# 百度日配额按北京时间零点重置。用固定 UTC+8 而不是 zoneinfo，
# Windows 上没装 tzdata 时 zoneinfo 找不到 Asia/Shanghai。
_BEIJING = timezone(timedelta(hours=8))


def _beijing_day() -> str:
    return datetime.now(_BEIJING).date().isoformat()


def _endpoint_label(endpoint: str) -> str:
    """把接口路径归并为短名，供配额统计按接口类别展示。"""
    if "/place/v2/search" in endpoint:
        return "poi"
    if "reverse_geocoding" in endpoint:
        return "regeo"
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
        # AK 延迟到真正发请求时才校验：没配 AK 时后端仍要能启动，
        # 让评审零配置也能打开预生成样例、离线模拟与导出。
        self._ak = self._s.server_ak
        self._bucket = TokenBucket(self._s.max_qps)
        self._cache = DiskCache(self._s.cache_dir, self._s.cache_grid_m)
        self._client: httpx.AsyncClient | None = None
        self._matrix_gate = asyncio.Semaphore(max(1, self._s.matrix_concurrency))
        # 当前与历史最高的在途矩阵请求数，用于验证并发确实生效、且没有突破上限
        self.matrix_inflight = 0
        self.matrix_inflight_peak = 0
        self._route_gate = asyncio.Semaphore(max(1, self._s.route_concurrency))
        # 步行路线规划失败的记录。路线只用于校正过街等待与围挡，失败时该方向
        # 退回批量算路的耗时并在结果里标注，不让整个分析失败。
        self.route_failures: list[str] = []
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
        # 配额一旦耗尽，同一自然日内不再重复试探该接口，避免每次调用都白等一轮重试。
        # 记下状态码与北京时间日期：302 在次日 0 点重置，常驻进程过了零点必须自动解除，
        # 否则后端不重启就一直拒绝；301 是永久超限，进程内一直保持。
        self._exhausted: dict[str, tuple[int, str]] = {}
        # 配额耗尽事件（每个接口每天记一次），供分析结果写出具体的降级说明
        self.quota_events: list[dict[str, Any]] = []
        # 驾车实时路况：本次用到的每个点对的路况年龄（秒），以及因接口失败
        # 改用过期缓存兜底的点对年龄。路况会过时，结果里必须写明是多久以前的
        self.traffic_ages: list[float] = []
        self.traffic_stale: list[float] = []
        self.traffic_stale_reason: str | None = None

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
            "route_failures": len(self.route_failures),
            "quota_events": len(self.quota_events),
            "traffic_ages": len(self.traffic_ages),
            "traffic_stale": len(self.traffic_stale),
            "requests": dict(self.request_counts),
        }

    def _ttl(self, mode: TravelMode | None = None) -> float:
        """缓存新鲜期。实时路况单独配置（默认 10 分钟），其余默认 30 天。"""
        if mode is not None and mode.uses_traffic:
            return self._s.traffic_ttl_s
        return self._s.cache_ttl_s

    @asynccontextmanager
    async def _matrix_slot(self) -> AsyncIterator[None]:
        """占一个批量算路的在途名额，并记录在途峰值。"""
        async with self._matrix_gate:
            self.matrix_inflight += 1
            self.matrix_inflight_peak = max(self.matrix_inflight_peak, self.matrix_inflight)
            try:
                yield
            finally:
                self.matrix_inflight -= 1

    def _backoff(self, attempt: int) -> float:
        """指数退避加 ±50% 抖动。并发的几个请求同时撞上 401 时，
        不加抖动会同一时刻醒来、再一起撞一次。"""
        return self._s.retry_backoff * (2**attempt) * (0.5 + random.random())

    def _mark_exhausted(self, endpoint: str, status: int) -> None:
        if endpoint in self._exhausted:
            return
        self._exhausted[endpoint] = (status, _beijing_day())
        self.quota_events.append(
            {
                "endpoint": endpoint,
                "service": service_label(endpoint),
                "status": status,
                "day": _beijing_day(),
            }
        )

    def _check_exhausted(self, endpoint: str) -> None:
        hit = self._exhausted.get(endpoint)
        if hit is None:
            return
        status, day = hit
        if status == 302 and day != _beijing_day():
            # 过了北京时间零点，当日配额已重置
            del self._exhausted[endpoint]
            return
        raise QuotaExhaustedError(status, "今天已确认配额耗尽，本进程不再重复请求", endpoint)

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
        if not self._ak or self._ak.startswith("your_"):
            raise ConfigurationError(5, "未配置 BAIDU_SERVER_AK", endpoint)
        self._check_exhausted(endpoint)

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
                    self._mark_exhausted(endpoint, status)
                    raise error
                if isinstance(error, ConfigurationError):
                    raise error
                if status in RETRYABLE_STATUS and attempt < self._s.max_retries:
                    last_error = error
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                raise error
            except (QuotaExhaustedError, ConfigurationError):
                raise
            except (httpx.HTTPError, ValueError) as exc:
                # 网络抖动与 JSON 解析失败同属瞬时故障。实测本机走系统代理，
                # 偶发 ConnectError 属常态，必须重试而非上抛。
                last_error = exc
                if attempt < self._s.max_retries:
                    await asyncio.sleep(self._backoff(attempt))
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
        scope: int = 1,
    ) -> list[dict[str, Any]] | None:
        """周边检索单页。返回 None 表示查询失败或配额耗尽——调用方必须区别对待，
        绝不能当成「该区域没有此类设施」，否则 API 故障会被伪装成服务盲区。

        scope=2 时每条结果多一个 detail_info：分类标签、导航点（navi_location，
        导航实际引到的位置，通常是大门）和子点（children：学校的各个门、停车场……）。
        请求次数不变。缓存键只在 scope≠1 时带上它，已有的 scope=1 缓存照常命中。
        """
        extra = (scope,) if scope != 1 else ()
        cache_path = self._cache.key_for_point("poi", lat, lng, keyword, radius, page_num, *extra)
        cached = self._cache.read(cache_path, self._ttl())
        if cached is not None:
            return cached["results"]

        params: dict[str, Any] = {
            "query": keyword,
            "location": f"{lat:.6f},{lng:.6f}",
            "radius": radius,
            "output": "json",
            "page_size": page_size,
            "page_num": page_num,
        }
        if scope != 1:
            params["scope"] = scope
        try:
            body = await self._request("/place/v2/search", params)
        except ConfigurationError:
            # AK 或白名单错误是部署问题，必须原样上报；
            # 吞掉它会让六个品类全部显示「查询失败」，用户无从知道是 AK 配错了
            raise
        except (BaiduApiError, httpx.HTTPError, ValueError):
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
        scope: int = 1,
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
                keyword, lat, lng, radius, page_size=page_size, page_num=page, scope=scope
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
        dest_grid_m: float | None = None,
    ) -> list[list[dict[str, float] | None]]:
        return await self.route_matrix_grid("walk", origins, destinations, dest_grid_m)

    async def walking_route(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        fresh: bool = False,
    ) -> dict[str, Any] | None:
        """步行路线规划（directionlite/v1/walking）：一条路线的分段、耗时与折线。

        批量算路给的耗时只是距离 ÷ 步速，路线规划在过马路、主干道路段的步骤里
        带额外秒数，并返回每段折线。前者用于校正过街等待，后者用于判定是否穿过围挡。

        fresh=True 时跳过缓存直接问百度（复测巡检用：要的就是「现在」的路线），
        结果照常写回缓存，之后的分析也用上最新路网。

        返回 None 表示查询失败（含配额耗尽、服务未开通）。路线只是增强信息，
        失败时调用方退回批量算路的耗时并标注，不让整个分析失败。
        """
        cache_path = self._cache.key_for_pair(
            "route_walk", origin[0], origin[1], destination[0], destination[1]
        )
        if not fresh:
            cached = self._cache.read(cache_path, self._ttl())
            if cached is not None:
                if "fetched_at" not in cached:
                    # 旧缓存条目没记取得时间，用文件写入时间补上，复测时基线年龄才对得上
                    age = self._cache.age_s(cache_path)
                    if age is not None:
                        cached["fetched_at"] = (
                            datetime.now(_BEIJING) - timedelta(seconds=age)
                        ).isoformat(timespec="seconds")
                return cached

        try:
            async with self._route_gate:
                body = await self._request(
                    "/directionlite/v1/walking",
                    {
                        "origin": f"{origin[0]:.6f},{origin[1]:.6f}",
                        "destination": f"{destination[0]:.6f},{destination[1]:.6f}",
                    },
                )
        except (BaiduApiError, httpx.HTTPError, ValueError) as exc:
            self.route_failures.append(f"{type(exc).__name__}: {exc}")
            return None

        routes = (body.get("result") or {}).get("routes") or []
        if not routes:
            self.route_failures.append("路线规划未返回路线")
            return None
        route = routes[0]
        steps = []
        for step in route.get("steps") or []:
            steps.append(
                {
                    "distance_m": float(step.get("distance") or 0),
                    "duration_s": float(step.get("duration") or 0),
                    "turn_type": str(step.get("turn_type") or ""),
                    "instruction": _TAG_RE.sub("", str(step.get("instruction") or "")),
                    "path": [
                        [round(lat, 6), round(lng, 6)]
                        for lat, lng in decode_baidu_path(str(step.get("path") or ""))
                    ],
                }
            )
        entry = {
            "distance_m": float(route.get("distance") or 0),
            "duration_s": float(route.get("duration") or 0),
            "steps": steps,
            # 复测巡检要知道基线是哪天取的；旧缓存条目没有这个字段
            "fetched_at": datetime.now(_BEIJING).isoformat(timespec="seconds"),
        }
        self._cache.write(cache_path, entry)
        return entry

    async def reverse_geocode(self, lat: float, lng: float) -> dict[str, str] | None:
        """逆地理编码：坐标 → 「XX 路 XX 号附近」。只用于给选址建议写一个看得懂的位置。

        失败返回 None，调用方只是少一行地址，不影响选址结论。缓存 30 天。
        """
        cache_path = self._cache.key_for_point("regeo", lat, lng)
        cached = self._cache.read(cache_path, self._ttl())
        if cached is not None:
            return cached
        try:
            body = await self._request(
                "/reverse_geocoding/v3/",
                {"location": f"{lat:.6f},{lng:.6f}", "output": "json", "coordtype": "bd09ll"},
            )
        except ConfigurationError:
            raise
        except (BaiduApiError, httpx.HTTPError, ValueError):
            return None
        result = body.get("result") or {}
        comp = result.get("addressComponent") or {}
        entry = {
            "address": str(result.get("formatted_address") or ""),
            "description": str(result.get("sematic_description") or ""),
            "street": str(comp.get("street") or ""),
            "district": str(comp.get("district") or ""),
            "city": str(comp.get("city") or ""),
        }
        self._cache.write(cache_path, entry)
        return entry

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

        # 配额耗尽与配置错误必须上抛。旧做法把它们和普通失败一样填成 None，
        # 等时圈会把整批点当成「障碍」，得出一个面积为 0 的圈并照常出分——
        # 接口故障被伪装成了「这里走不出去」。
        for chunk in chunks:
            if isinstance(chunk, QuotaExhaustedError | ConfigurationError):
                raise chunk

        out: list[dict[str, float] | None] = []
        for batch, chunk in zip(batches, chunks, strict=True):
            if isinstance(chunk, BaseException):
                self.matrix_failures.append(
                    f"{mode.id} 1x{len(batch)} {type(chunk).__name__}: {chunk}"
                )
                out.extend([None] * len(batch))
            else:
                out.extend(chunk)
        return out

    def _pair_key(
        self,
        mode: TravelMode,
        o: tuple[float, float],
        d: tuple[float, float],
        dest_grid_m: float | None,
    ) -> Any:
        """点对缓存键。dest_grid_m 给定时，终点另按这个更细的网格量化并加进键里。

        默认的 50 米量化对网格中心与设施点够用；学校的几个门常常相距只有二三十米，
        按 50 米量化会落进同一格、共用一个测距结果，按门测距就白做了。
        只给入口终点加细键，已有的 50 米缓存照常命中。
        """
        if not dest_grid_m:
            return self._cache.key_for_pair(mode.cache_ns, o[0], o[1], d[0], d[1])
        step = dest_grid_m / METERS_PER_DEGREE
        return self._cache.key_for_pair(
            mode.cache_ns,
            o[0],
            o[1],
            d[0],
            d[1],
            "d",
            f"{dest_grid_m:g}",
            round(d[0] / step),
            round(d[1] / step),
        )

    async def route_matrix_grid(
        self,
        mode_id: str,
        origins: Sequence[tuple[float, float]],
        destinations: Sequence[tuple[float, float]],
        dest_grid_m: float | None = None,
    ) -> list[list[dict[str, float] | None]]:
        """多起点 × 多终点矩阵。乘积上限随出行方式变化（骑行文档为 50）。

        **只请求缓存里没有的点对**（配额按点对计）：先逐个查缓存，再把「缺的终点完全相同」
        的起点并成一组，每组正好是一个缺失的矩形，切成不超过乘积上限的块发出。
        盲区判定的「多起点 × 1 终点」退化成「只发没测过的那几个起点」。
        旧做法是先切块、块里缺一个就整块重发，叠加标注的第二遍会把测过的点对再买一遍。

        配额耗尽与配置错误向上抛，调用方据此改用估算或记为未知；其余失败（限流重试耗尽、
        网络中断）只让那一块记为 None 并留痕，不影响别的块。
        """
        if not origins or not destinations:
            return [[None] * len(destinations) for _ in origins]

        mode = get_mode(mode_id)
        ttl = self._ttl(mode)
        out: list[list[dict[str, float] | None]] = [
            [self._cache.read(self._pair_key(mode, o, d, dest_grid_m), ttl) for d in destinations]
            for o in origins
        ]

        # 缺的终点集合相同的起点并成一组：组内「起点 × 缺的终点」全是没测过的点对
        groups: dict[tuple[int, ...], list[int]] = {}
        for i, row in enumerate(out):
            missing = tuple(j for j, entry in enumerate(row) if entry is None)
            if missing:
                groups.setdefault(missing, []).append(i)
        if not groups:
            return out

        budget = max(1, min(self._s.matrix_batch_size, mode.matrix_product_limit))
        blocks: list[tuple[list[int], list[int]]] = []
        for cols, rows in groups.items():
            d_size = min(len(cols), budget)
            o_size = max(1, budget // d_size)
            for oi in range(0, len(rows), o_size):
                for di in range(0, len(cols), d_size):
                    blocks.append((rows[oi : oi + o_size], list(cols[di : di + d_size])))

        results = await asyncio.gather(
            *(
                self._matrix_block(
                    mode,
                    [origins[i] for i in rows],
                    [destinations[j] for j in cols],
                    dest_grid_m,
                )
                for rows, cols in blocks
            ),
            return_exceptions=True,
        )
        fatal: BaseException | None = None
        for (rows, cols), result in zip(blocks, results, strict=True):
            if isinstance(result, BaseException):
                # 先让所有块落定再抛：别的块已经测到的点对照样写进缓存，下次不必重测
                if fatal is None or (
                    isinstance(result, QuotaExhaustedError | ConfigurationError)
                    and not isinstance(fatal, QuotaExhaustedError | ConfigurationError)
                ):
                    fatal = result
                continue
            for a, i in enumerate(rows):
                for b, j in enumerate(cols):
                    out[i][j] = result[a][b]
        if fatal is not None:
            raise fatal
        return out

    async def _matrix_block(
        self,
        mode: TravelMode,
        origins: Sequence[tuple[float, float]],
        destinations: Sequence[tuple[float, float]],
        dest_grid_m: float | None = None,
    ) -> list[list[dict[str, float] | None]]:
        """请求一个矩形块（调用方已确认这些点对都不在缓存里），结果逐个点对写入缓存。

        缓存命名空间含出行方式，步行旧缓存仍可复用。
        """
        block: list[list[dict[str, float] | None]] = [[None] * len(destinations) for _ in origins]
        # 当天已确认配额耗尽：直接抛出，也不把没发出去的点对记进「实发」
        self._check_exhausted(mode.endpoint)
        n_pairs = len(origins) * len(destinations)
        self.matrix_pairs += n_pairs
        params: dict[str, Any] = {
            "origins": "|".join(f"{o[0]:.6f},{o[1]:.6f}" for o in origins),
            "destinations": "|".join(f"{d[0]:.6f},{d[1]:.6f}" for d in destinations),
            "output": "json",
            **{k: v for k, v in mode.extra_params.items()},
        }
        try:
            async with self._matrix_slot():
                body = await self._request(mode.endpoint, params, cost=n_pairs / PAIRS_PER_TOKEN)
        except (ConfigurationError, QuotaExhaustedError):
            # 配额耗尽不能吞成「测距失败」：调用方要据此改用估算并如实说明，
            # 吞掉的话结果只会显示「若干格未核验」，看不出是配额用完了
            raise
        except BaiduApiError as exc:
            self.matrix_failures.append(
                f"{mode.id} {len(origins)}x{len(destinations)} status={exc.status} {exc}"
            )
            return block
        except (httpx.HTTPError, ValueError) as exc:
            self.matrix_failures.append(
                f"{mode.id} {len(origins)}x{len(destinations)} {type(exc).__name__}: {exc}"
            )
            return block

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
                block[i][j] = entry
                self._cache.write(self._pair_key(mode, o, d, dest_grid_m), entry)
        return block

    def _stale_traffic(
        self, mode: TravelMode, paths: list, misses: list[int]
    ) -> dict[int, tuple[dict[str, float], float]] | None:
        """取过期的路况缓存兜底。只要有一个点没有旧数据就放弃：
        缺的点会被等时圈当成障碍，混用反而更糟。"""
        out: dict[int, tuple[dict[str, float], float]] = {}
        for slot in misses:
            entry = self._cache.read(paths[slot])  # 不设有效期
            age = self._cache.age_s(paths[slot])
            if entry is None or age is None:
                return None
            out[slot] = (entry, age)
        return out

    async def _route_matrix_batch(
        self,
        mode: TravelMode,
        origin: tuple[float, float],
        destinations: Sequence[tuple[float, float]],
    ) -> list[dict[str, float] | None]:
        results: list[dict[str, float] | None] = [None] * len(destinations)
        misses: list[int] = []
        ttl = self._ttl(mode)
        paths = [
            self._cache.key_for_pair(mode.cache_ns, origin[0], origin[1], d[0], d[1])
            for d in destinations
        ]
        fresh_ages: list[float] = []
        for i, path in enumerate(paths):
            cached = self._cache.read(path, ttl)
            if cached is not None:
                results[i] = cached
                if mode.uses_traffic:
                    fresh_ages.append(self._cache.age_s(path) or 0.0)
            else:
                misses.append(i)

        if not misses:
            self.traffic_ages.extend(fresh_ages)
            return results

        dest_param = "|".join(f"{destinations[i][0]:.6f},{destinations[i][1]:.6f}" for i in misses)
        params: dict[str, Any] = {
            "origins": f"{origin[0]:.6f},{origin[1]:.6f}",
            "destinations": dest_param,
            "output": "json",
            **{k: v for k, v in mode.extra_params.items()},
        }
        try:
            # 当天已确认配额耗尽时不发请求，也不把没发出去的点对记进「实发」；
            # 放在 try 里，实时路况仍能走下面的过期缓存兜底
            self._check_exhausted(mode.endpoint)
            self.matrix_pairs += len(misses)
            async with self._matrix_slot():
                body = await self._request(
                    mode.endpoint,
                    params,
                    cost=len(misses) / PAIRS_PER_TOKEN,
                )
        except ConfigurationError:
            raise
        except (BaiduApiError, httpx.HTTPError, ValueError) as exc:
            # 实时路况「部分保留」：新鲜期（默认 10 分钟）过后条目并不删除。
            # 接口配额耗尽或网络失败时，用最近一次查到的路况兜底，并记下它有多旧——
            # 一张半小时前的路况图，比一个面积为 0 的圈或一条报错更有用，但必须标明。
            stale = self._stale_traffic(mode, paths, misses) if mode.uses_traffic else None
            if stale is None:
                raise
            for slot, (entry, age) in stale.items():
                results[slot] = entry
                self.traffic_stale.append(age)
            self.traffic_stale_reason = f"{type(exc).__name__}: {exc}"
            self.traffic_ages.extend(fresh_ages)
            self.traffic_ages.extend(age for _, age in stale.values())
            return results

        self.traffic_ages.extend(fresh_ages)
        if mode.uses_traffic:
            self.traffic_ages.extend(0.0 for _ in misses)
        for slot, item in zip(misses, body.get("result", []), strict=False):
            distance = item.get("distance", {}).get("value")
            duration = item.get("duration", {}).get("value")
            if distance is None or duration is None:
                continue
            entry = {"distance_m": float(distance), "duration_s": float(duration)}
            results[slot] = entry
            dest = destinations[slot]
            self._cache.write(
                self._cache.key_for_pair(mode.cache_ns, origin[0], origin[1], dest[0], dest[1]),
                entry,
            )
        return results
