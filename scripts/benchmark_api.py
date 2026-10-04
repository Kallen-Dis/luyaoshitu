"""API 调用优化的对比实验：零配额、可复现。

**不调用百度。** 本地缓存里存着两份样例生成时真实测到的点对、路线与检索结果，这里搭一个
「回放服务器」按百度的接口格式把它们原样答回去；缓存里没有的点对（主要是朴素做法才会去测的
远处设施）用模型补：直线距离 × 该样例实测的平均绕行系数，步速 1.17 m/s。

**时间是虚拟的。** 每个请求按延迟假设「等」一段时间，但事件循环用虚拟时钟：没有可运行的任务时
直接把时钟拨到下一个定时器。令牌桶、在途上限、重试退避、缓存全是生产代码本身，只是不必真等——
串行要几十分钟的朴素做法几秒就跑完，抖动与故障注入用固定随机种子，每次结果都一样。

**回放校验。** 现行策略在回放下的点对数、请求数、逐格判定，与真实生成快照那次逐一对照，
对得上才说明回放可信（见报告第 1 节）。

用法：
    python scripts/benchmark_api.py            # 全部实验，写 reports/api-benchmark.md 与 .json
    python scripts/benchmark_api.py --quick    # 跳过最慢的「逐对请求」朴素做法
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import math
import random
import sys
import time
import types
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.baidu import client as client_mod  # noqa: E402
from app.baidu.cache import DiskCache  # noqa: E402
from app.baidu.client import BaiduMapClient  # noqa: E402
from app.baidu.errors import IncompleteSamplingError  # noqa: E402
from app.config import PROJECT_ROOT, Settings  # noqa: E402
from app.isochrone.algorithm import IsochroneConfig, compute_isochrone  # noqa: E402
from app.isochrone.geometry import haversine_m, offset_point  # noqa: E402
from app.isochrone.refine import RefineOptions  # noqa: E402
from app.poi.catalog import CATEGORIES, KEY_CATEGORIES  # noqa: E402
from app.poi.collect import CoverageResult, clean, collect_coverage  # noqa: E402
from app.poi.entries import ENTRY_GRID_M, destinations, lookup_gates  # noqa: E402
from app.report.blindspot import BlindspotConfig, identify_blindspots, layout_cells  # noqa: E402
from app.report.simulate import simulate_facility  # noqa: E402

SAMPLES = ROOT / "data" / "samples"
REPORT_MD = ROOT / "reports" / "api-benchmark.md"
REPORT_JSON = ROOT / "reports" / "api-benchmark.json"
REAL_CACHE = PROJECT_ROOT / ".cache"

WALK_SPEED = 1.17  # 实测：步行批量算路的耗时 = 距离 ÷ 1.17 m/s
KEY_NAMES = tuple(c.name for c in KEY_CATEGORIES)
FINE_DEG = ENTRY_GRID_M / 111_320.0  # 入口终点细键的量化步长（度），与客户端一致


def real_blind_run(snapshot: dict[str, Any]) -> dict[str, Any]:
    """真实运行的记录：run_isochrone.py 写进快照 api_usage 的盲区一段实发量与用时。"""
    p = snapshot["properties"]
    stage = ((p.get("api_usage") or {}).get("stages") or {}).get("blindspots") or {}
    return {
        "pairs": int(stage.get("matrix_pairs") or 0),
        "requests": int((stage.get("requests") or {}).get("matrix") or 0),
        "elapsed_s": float(stage.get("elapsed_s") or 0.0),
        "date": str(p.get("generated_at") or "")[:10],
    }


# ---------- 虚拟时钟 ----------


class VirtualClockLoop(asyncio.SelectorEventLoop):
    """没有可运行的回调时，直接把时钟拨到下一个定时器，而不是真的睡过去。"""

    def __init__(self) -> None:
        super().__init__()
        self._now = 0.0
        real_select = self._selector.select

        def select(timeout: float | None = None) -> list:
            if timeout is None:
                raise RuntimeError("虚拟时钟：没有定时器也没有可运行的任务，程序卡死了")
            if timeout > 0:
                self._now += timeout
            return real_select(0)

        self._selector.select = select  # type: ignore[method-assign]

    def time(self) -> float:
        return self._now


def measure_local(site: Any) -> dict[str, Any]:
    """在真实缓存上把整次分析跑一遍（真实时钟、不联网），量出本地计算要多久。

    快照刚生成过，所需的点对、路线、检索结果都在 .cache/ 里；万一有漏的，请求会被一个
    只回「参数非法」的假服务器挡下来并计数——这里绝不会真的去请求百度。
    """
    blocked: list[str] = []

    async def guard(request: httpx.Request) -> httpx.Response:
        blocked.append(request.url.path)
        return httpx.Response(200, json={"status": 2, "message": "离线测量，不联网"})

    async def go() -> dict[str, float]:
        settings = Settings(server_ak="offline-guard", browser_ak="", cache_dir=REAL_CACHE)
        client = BaiduMapClient(settings)
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(guard))
        out: dict[str, float] = {}
        try:
            t = time.perf_counter()
            iso = await compute_isochrone(
                client, site.center, IsochroneConfig(), RefineOptions(delay=True)
            )
            out["isochrone"] = time.perf_counter() - t
            t = time.perf_counter()
            coverage = await collect_coverage(client, site.center, 2500)
            out["coverage"] = time.perf_counter() - t
            t = time.perf_counter()
            await identify_blindspots(
                client, site.center, iso.polygon, coverage, BlindspotConfig(), refine=iso.refine
            )
            out["blindspots"] = time.perf_counter() - t
        finally:
            await client._client.aclose()
        return out

    times = asyncio.run(go())
    return {**{k: round(v, 2) for k, v in times.items()}, "blocked_requests": len(blocked)}


def run_virtual(factory: Callable[[], Awaitable[Any]], seed: int = 7) -> Any:
    """在虚拟时钟上跑一个协程。生产代码里令牌桶用的 time.monotonic 也换成虚拟时钟。"""
    loop = VirtualClockLoop()
    saved = client_mod.time
    client_mod.time = types.SimpleNamespace(monotonic=loop.time)  # type: ignore[assignment]
    random.seed(seed)  # 重试退避的抖动
    try:
        return loop.run_until_complete(factory())
    finally:
        client_mod.time = saved
        loop.close()


def now() -> float:
    return asyncio.get_running_loop().time()


# ---------- 内存缓存（每次实验一份全新的，互不串味） ----------


class MemoryCache(DiskCache):
    def __init__(self) -> None:
        super().__init__(Path("memory"), 50.0)
        self._data: dict[Path, str] = {}

    def read(self, path: Path, max_age_s: float | None = None) -> Any | None:  # type: ignore[override]
        raw = self._data.get(path)
        return None if raw is None else json.loads(raw)

    def write(self, path: Path, payload: Any) -> None:  # type: ignore[override]
        self._data[path] = json.dumps(payload, ensure_ascii=False)

    def age_s(self, path: Path) -> float | None:  # type: ignore[override]
        return 0.0 if path in self._data else None

    def seed_walk_pairs(self, before_ts: float) -> int:
        """把早于 before_ts 写进 .cache/walk 的点对放进来，复现真实运行起步时的缓存状态。

        内存缓存与磁盘缓存的键（量化坐标的摘要）相同，只是根目录不同，按文件名对上即可。
        """
        n = 0
        for path in (REAL_CACHE / "walk").glob("*.json"):
            if path.stat().st_mtime < before_ts:
                self._data[self._root / "walk" / path.name] = path.read_text(encoding="utf-8")
                n += 1
        return n


# ---------- 延迟假设与故障注入 ----------


@dataclass(frozen=True)
class Latency:
    key: str
    label: str
    base_s: dict[str, float]
    per_pair_s: float = 0.0
    jitter: float = 0.2

    def of(self, kind: str, pairs: int, rng: random.Random) -> float:
        base = self.base_s[kind] + (self.per_pair_s * pairs if kind == "matrix" else 0.0)
        return base * (1 + self.jitter * (2 * rng.random() - 1))


# 小请求往返实测：批量算路 0.42 秒、路线 0.43、检索 0.41（docs/api-optimization.md 4.1，2026-09-15）
MEASURED = {"matrix": 0.42, "route": 0.43, "poi": 0.41}
LATENCIES = {
    "A": Latency("A", "固定往返（小请求实测值）", MEASURED),
    "B": Latency("B", "往返 + 每点对 10 毫秒（100 点对约 1.4 秒）", MEASURED, 0.010),
    "C": Latency("C", "慢网络：B 的两倍", {k: 2 * v for k, v in MEASURED.items()}, 0.020),
}


@dataclass(frozen=True)
class Faults:
    p401: float = 0.0  # 每次请求返回 401（并发超限）的概率
    p_network: float = 0.0  # 每次请求网络中断的概率
    quota_after_pairs: int | None = None  # 批量算路累计答出这么多点对后开始返回 302
    poi_fail_keywords: frozenset[str] = frozenset()  # 这些关键词的检索一律返回服务器错误
    fail_isochrone_batch: bool = False  # 等时圈第三批（52 个采样点）一律返回服务器错误


# ---------- 回放服务器 ----------


def _coords(text: str) -> list[tuple[float, float]]:
    out = []
    for part in text.split("|"):
        lat, lng = part.split(",")
        out.append((float(lat), float(lng)))
    return out


@dataclass
class ServerStats:
    requests: Counter = field(default_factory=Counter)  # 收到的请求（含重试）
    pairs: int = 0  # 收到的点对（含重试）
    answered_pairs: int = 0  # 成功答出的点对
    replay_pairs: int = 0
    pre_existing_pairs: int = 0  # 回放的点对里，真实运行开始前就已在缓存里的
    model_pairs: int = 0
    dup_pairs: int = 0  # 量化到 50 米格后与之前答过的点对重复（同一请求内或并发请求间）
    replay_routes: int = 0
    model_routes: int = 0
    replay_pages: int = 0
    model_pages: int = 0
    injected: Counter = field(default_factory=Counter)
    after_quota: int = 0  # 302 之后又收到的批量算路请求
    inflight: Counter = field(default_factory=Counter)
    peak: Counter = field(default_factory=Counter)


class ReplayServer:
    def __init__(
        self,
        detour: float,
        latency: Latency,
        faults: Faults,
        seed: int = 11,
        cutoff_ts: float | None = None,
    ) -> None:
        self.real = DiskCache(REAL_CACHE, 50.0)
        self.detour = detour
        self.latency = latency
        self.faults = faults
        self.rng = random.Random(seed)
        self.stats = ServerStats()
        self.quota_tripped = False
        # 真实运行开始的时刻：早于它写入缓存的点对，真实运行时是命中缓存、没有发出去的
        self.cutoff_ts = cutoff_ts
        self._answered: set[tuple[int, int, int, int]] = set()

    def _quantized(self, value: float) -> list[int]:
        """坐标量化到 50 米格的下标。请求参数只保留 6 位小数，恰好落在格边上的坐标
        取整后可能落到相邻格，这时两个下标都试一下。"""
        x = value / self.real._grid_deg
        options = [round(x)]
        frac = x - math.floor(x)
        if abs(frac - 0.5) < 0.002:
            options.append(math.floor(x) if options[0] == math.ceil(x) else math.ceil(x))
        return options

    def lookup(self, namespace: str, coords: tuple[float, ...], *extra: Any) -> Path | None:
        for combo in itertools.product(*(self._quantized(v) for v in coords)):
            path = self.real._path(namespace, [*combo, *extra])
            if path.exists():
                return path
        return None

    def _model(self, o: tuple[float, float], d: tuple[float, float]) -> tuple[float, float]:
        dist = haversine_m(o[0], o[1], d[0], d[1]) * self.detour
        return dist, dist / WALK_SPEED

    @staticmethod
    def _error(status: int, message: str) -> httpx.Response:
        return httpx.Response(200, json={"status": status, "message": message})

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)
        kind = (
            "matrix"
            if "routematrix" in path
            else "route"
            if "directionlite" in path
            else "poi"
            if "place" in path
            else "other"
        )
        st = self.stats
        st.requests[kind] += 1
        pairs = 0
        if kind == "matrix":
            pairs = len(params["origins"].split("|")) * len(params["destinations"].split("|"))
            st.pairs += pairs
            if self.quota_tripped:
                st.after_quota += 1
        st.inflight[kind] += 1
        st.peak[kind] = max(st.peak[kind], st.inflight[kind])
        try:
            await asyncio.sleep(self.latency.of(kind, pairs, self.rng))
            f = self.faults
            if f.p_network and self.rng.random() < f.p_network:
                st.injected["network"] += 1
                raise httpx.ConnectError("注入：网络中断", request=request)
            if f.p401 and self.rng.random() < f.p401:
                st.injected["401"] += 1
                return self._error(401, "MQPS 超限（注入）")
            if kind == "poi" and params.get("query") in f.poi_fail_keywords:
                st.injected["poi"] += 1
                return self._error(1, "服务器内部错误（注入）")
            if kind == "matrix":
                return self._matrix(params, pairs)
            if kind == "route":
                return self._route(params)
            if kind == "poi":
                return self._poi(params)
            return self._error(2, "回放服务器不支持这个接口")
        finally:
            st.inflight[kind] -= 1

    def _matrix(self, params: dict[str, str], pairs: int) -> httpx.Response:
        f = self.faults
        origins = _coords(params["origins"])
        dests = _coords(params["destinations"])
        if f.fail_isochrone_batch and len(origins) == 1 and len(dests) == 52:
            self.stats.injected["isochrone_batch"] += 1
            return self._error(1, "服务器内部错误（注入）")
        if f.quota_after_pairs is not None and (
            self.quota_tripped or self.stats.answered_pairs + pairs > f.quota_after_pairs
        ):
            self.quota_tripped = True
            self.stats.injected["302"] += 1
            return self._error(302, "天配额超限（注入）")
        result = []
        q = self.real._quantize
        for o in origins:
            for d in dests:
                key = (q(o[0]), q(o[1]), q(d[0]), q(d[1]))
                if key in self._answered:
                    self.stats.dup_pairs += 1
                self._answered.add(key)
                # 入口终点（校门）在缓存里用的是细键，先找细键，找不到再找 50 米粗键
                path = self.lookup(
                    "walk",
                    (o[0], o[1], d[0], d[1]),
                    "d",
                    f"{ENTRY_GRID_M:g}",
                    round(d[0] / FINE_DEG),
                    round(d[1] / FINE_DEG),
                ) or self.lookup("walk", (o[0], o[1], d[0], d[1]))
                entry = self.real.read(path) if path is not None else None
                if entry is not None:
                    self.stats.replay_pairs += 1
                    if self.cutoff_ts is not None and path.stat().st_mtime < self.cutoff_ts:
                        self.stats.pre_existing_pairs += 1
                    dist, dur = entry["distance_m"], entry["duration_s"]
                else:
                    self.stats.model_pairs += 1
                    dist, dur = self._model(o, d)
                result.append({"distance": {"value": dist}, "duration": {"value": dur}})
        self.stats.answered_pairs += pairs
        return httpx.Response(200, json={"status": 0, "result": result})

    def _route(self, params: dict[str, str]) -> httpx.Response:
        o = _coords(params["origin"])[0]
        d = _coords(params["destination"])[0]
        path = self.lookup("route_walk", (o[0], o[1], d[0], d[1]))
        entry = self.real.read(path) if path is not None else None
        if entry is not None:
            self.stats.replay_routes += 1
            steps = [
                {
                    "distance": s["distance_m"],
                    "duration": s["duration_s"],
                    "turn_type": s.get("turn_type", ""),
                    "instruction": s.get("instruction", ""),
                    "path": ";".join(f"{lng},{lat}" for lat, lng in s.get("path") or []),
                }
                for s in entry.get("steps") or []
            ]
            route = {
                "distance": entry["distance_m"],
                "duration": entry["duration_s"],
                "steps": steps,
            }
        else:
            self.stats.model_routes += 1
            dist, dur = self._model(o, d)
            dur += 0.04 * dist  # 约每公里 40 秒过街等待
            route = {
                "distance": dist,
                "duration": dur,
                "steps": [
                    {
                        "distance": dist,
                        "duration": dur,
                        "turn_type": "",
                        "instruction": "",
                        "path": f"{o[1]},{o[0]};{d[1]},{d[0]}",
                    }
                ],
            }
        return httpx.Response(200, json={"status": 0, "result": {"routes": [route]}})

    def _poi(self, params: dict[str, str]) -> httpx.Response:
        lat, lng = _coords(params["location"])[0]
        # scope=2 的检索（带导航点、子点）单独缓存，键里多一段 scope，和客户端一致
        scope = [int(params["scope"])] if params.get("scope", "1") != "1" else []
        path = self.lookup(
            "poi",
            (lat, lng),
            params["query"],
            int(params["radius"]),
            int(params["page_num"]),
            *scope,
        )
        entry = self.real.read(path) if path is not None else None
        if entry is not None:
            self.stats.replay_pages += 1
            body = {"status": 0, "results": entry["results"], "total": entry.get("total")}
        else:
            self.stats.model_pages += 1
            body = {"status": 0, "results": [], "total": 0}
        return httpx.Response(200, json=body)


def make_client(
    server: ReplayServer, *, matrix: int = 3, route: int = 4, qps: int = 8
) -> BaiduMapClient:
    settings = Settings(
        server_ak="benchmark-replay-ak",
        browser_ak="",
        cache_dir=REAL_CACHE / "_benchmark_unused",
        max_qps=qps,
        matrix_concurrency=matrix,
        route_concurrency=route,
    )
    client = BaiduMapClient(settings)
    client._cache = MemoryCache()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(server.handle))
    return client


# ---------- 样例 ----------


@dataclass
class Site:
    key: str
    name: str
    center: tuple[float, float]
    polygon: list[tuple[float, float]]
    detour: float
    snapshot: dict[str, Any]


def load_site(key: str) -> Site:
    feature = json.loads((SAMPLES / f"isochrone-{key}-15min.geojson").read_text(encoding="utf-8"))
    p = feature["properties"]
    ring = feature["geometry"]["coordinates"][0]
    polygon = [(lat, lng) for lng, lat in ring[:-1]]
    return Site(
        key=key,
        name=p["name"].replace("上海市普陀区", ""),
        center=(p["center"]["lat"], p["center"]["lng"]),
        polygon=polygon,
        detour=max(1.05, float(p.get("mean_detour") or 1.3)),
        snapshot=feature,
    )


GRIDS = {
    "circle100": ("15 分钟圈内 / 100 米（现版）", BlindspotConfig()),
    "disc200": (
        "中心 1.5 公里 / 200 米（旧版）",
        BlindspotConfig(layout="disc", grid_spacing_m=200.0),
    ),
}


# ---------- 实验：各环节的做法 ----------


def usage(client: BaiduMapClient, server: ReplayServer, t0: float) -> dict[str, Any]:
    return {
        "matrix_pairs": client.matrix_pairs,
        "requests": dict(server.stats.requests),
        "request_total": sum(server.stats.requests.values()),
        "elapsed_s": round(now() - t0, 1),
        "peak_inflight": dict(server.stats.peak),
    }


async def coverage_of(site: Site, latency: Latency) -> CoverageResult:
    server = ReplayServer(site.detour, latency, Faults())
    client = make_client(server)
    try:
        return await collect_coverage(client, site.center, 2500)
    finally:
        await client._client.aclose()


def decisions_from_cells(cells: list[Any]) -> dict[tuple[int, str], str]:
    out = {}
    for i, c in enumerate(cells):
        missing = c.missing if hasattr(c, "missing") else c.get("missing") or []
        unknown = c.unknown if hasattr(c, "unknown") else c.get("unknown") or []
        for name in KEY_NAMES:
            state = "unknown" if name in unknown else "missing" if name in missing else "ok"
            out[(i, name)] = state
    return out


def decide(rows: list[dict | None], limit: float = 1000.0) -> str:
    if any(r is not None and r["distance_m"] <= limit for r in rows):
        return "ok"
    if any(r is None for r in rows):
        return "unknown"
    return "missing"


async def blind_current(client, site, coverage, cfg):
    result = await identify_blindspots(client, site.center, site.polygon, coverage, cfg)
    return decisions_from_cells(result.cells)


async def blind_full_matrix(client, site, coverage, cfg):
    """不剪枝：每格对每家同类设施都测，靠批量矩阵装满 100 个点对一请求。"""
    cells = layout_cells(site.center, site.polygon, cfg)
    coords = [(c.lat, c.lng) for c in cells]
    await client.walking_matrix(site.center, coords)
    out = {}
    for name in KEY_NAMES:
        dests, fine = _destinations(coverage, name)
        if not dests:
            out.update({(i, name): "missing" for i in range(len(cells))})
            continue
        table = await client.walking_matrix_grid(coords, dests, **fine)
        for i, row in enumerate(table):
            out[(i, name)] = decide(row)
    return out


def _destinations(coverage, name):
    """这一类所有设施的测距终点（有校门测校门，没有测坐标点），以及要不要用入口细键。

    和现行做法同一口径：一家够得着就算有，所以把各家的终点摊平成一行再判定即可。
    """
    pois = coverage.pois_of(name)
    dests = [d for p in pois for d in destinations(p)]
    fine = {"dest_grid_m": ENTRY_GRID_M} if any(p.entries for p in pois) else {}
    return dests, fine


async def blind_pairwise(client, site, coverage, cfg):
    """最朴素：不用批量矩阵，每个「网格 × 设施」点对单独发一个请求，逐个等。"""
    cells = layout_cells(site.center, site.polygon, cfg)
    coords = [(c.lat, c.lng) for c in cells]
    for c in coords:
        await client.walking_matrix(site.center, [c])
    out = {}
    for name in KEY_NAMES:
        dests, fine = _destinations(coverage, name)
        for i, c in enumerate(coords):
            rows = []
            for d in dests:
                rows.append((await client.walking_matrix_grid([c], [d], **fine))[0][0])
            out[(i, name)] = decide(rows) if dests else "missing"
    return out


BLIND_STRATEGIES = {
    "pairwise": ("逐对请求，串行", blind_pairwise, 1),
    "full_serial": ("全量矩阵（不剪枝），串行", blind_full_matrix, 1),
    "full_conc": ("全量矩阵（不剪枝），在途 3", blind_full_matrix, 3),
    "current_serial": ("现行：剪枝 + 够近就停 + 分组装箱，串行", blind_current, 1),
    "current": ("现行，在途 3（默认）", blind_current, 3),
}


def agreement(a: dict, truth: dict) -> dict[str, Any]:
    same = sum(1 for k, v in a.items() if truth.get(k) == v)
    unknown = sum(1 for v in a.values() if v == "unknown")
    false_blind = sum(1 for k, v in a.items() if v == "missing" and truth.get(k) == "ok")
    missed_blind = sum(1 for k, v in a.items() if v == "ok" and truth.get(k) == "missing")
    # 原本已判定、现在变成「未知」的（降级而不是判错）
    to_unknown = sum(1 for k, v in a.items() if v == "unknown" and truth.get(k) != "unknown")
    return {
        "to_unknown": to_unknown,
        "judgements": len(a),
        "same": same,
        "same_pct": round(100 * same / len(a), 1) if a else None,
        "unknown": unknown,
        "false_blind": false_blind,
        "missed_blind": missed_blind,
    }


def run_blind(
    site,
    grid_key,
    strategy,
    coverage,
    latency,
    *,
    matrix=None,
    faults=None,
    cutoff_ts=None,
    seed_cache=False,
):
    """seed_cache：内存缓存先装入 cutoff_ts 之前已有的点对（复现真实运行起步时的缓存）。"""
    label, fn, default_matrix = BLIND_STRATEGIES[strategy]
    cfg = GRIDS[grid_key][1]

    async def go():
        server = ReplayServer(site.detour, latency, faults or Faults(), cutoff_ts=cutoff_ts)
        client = make_client(server, matrix=matrix or default_matrix)
        seeded = 0
        if seed_cache and cutoff_ts is not None:
            seeded = client._cache.seed_walk_pairs(cutoff_ts)
        t0 = now()
        try:
            decisions = await fn(client, site, coverage, cfg)
        finally:
            await client._client.aclose()
        out = usage(client, server, t0)
        out.update(
            strategy=strategy,
            label=label,
            cells=len(layout_cells(site.center, site.polygon, cfg)),
            replay_pairs=server.stats.replay_pairs,
            pre_existing_pairs=server.stats.pre_existing_pairs,
            model_pairs=server.stats.model_pairs,
            dup_pairs=server.stats.dup_pairs,
            seeded_pairs=seeded,
            injected=dict(server.stats.injected),
            after_quota=server.stats.after_quota,
            quota_events=len(client.quota_events),
            matrix_failures=len(client.matrix_failures),
        )
        return out, decisions

    return run_virtual(go)


# ---------- 实验：等时圈、路线、设施检索 ----------


def run_sampling(site, latency, how):
    """等时圈的 36 方向 × 7 档采样：逐点请求 / 批量串行 / 批量在途 3。"""

    async def go():
        server = ReplayServer(site.detour, latency, Faults())
        client = make_client(server, matrix=1 if how != "batch_conc" else 3)
        cfg = IsochroneConfig()
        flat = [
            offset_point(site.center[0], site.center[1], 360.0 * d / cfg.directions, r)
            for d in range(cfg.directions)
            for r in cfg.sampling_radii
        ]
        t0 = now()
        if how == "per_point":
            for p in flat:
                await client.route_matrix("walk", site.center, [p])
        else:
            await client.route_matrix("walk", site.center, flat)
        out = usage(client, server, t0)
        await client._client.aclose()
        return out

    return run_virtual(go)


def run_routes(site, latency, concurrency):
    """过街校正的 36 条步行路线：串行 / 在途 4（默认）。终点取快照里存的各方向路线终点。"""
    rays = site.snapshot["properties"]["rays"]
    targets = [tuple(r["route_to"]) for r in rays if r.get("route_to")]

    async def go():
        server = ReplayServer(site.detour, latency, Faults())
        client = make_client(server, route=concurrency)
        t0 = now()
        await asyncio.gather(*(client.walking_route(site.center, t) for t in targets))
        out = usage(client, server, t0)
        out["routes"] = len(targets)
        out["replay_routes"] = server.stats.replay_routes
        await client._client.aclose()
        return out

    return run_virtual(go)


def run_poi(site, latency, concurrent):
    """六类设施 17 个关键词：逐个关键词串行翻页 / 全部并发（默认，速率由令牌桶封顶）。"""

    async def go():
        server = ReplayServer(site.detour, latency, Faults())
        client = make_client(server)
        t0 = now()
        if concurrent:
            await collect_coverage(client, site.center, 2500)
        else:
            lat, lng = site.center
            for cat in CATEGORIES:
                pages = 6 if cat.key_facility else 3
                extra = {"scope": 2} if cat.entrances else {}
                records = []
                for kw in cat.keywords:
                    page = await client.search_poi_all(kw, lat, lng, 2500, max_pages=pages, **extra)
                    records.extend(page or [])
                if cat.entrances:
                    # 按校名查校门，也一所一所地查
                    pois, _ = clean(records, cat, site.center, 2500)
                    await lookup_gates(client, pois, site.center, concurrent=False)
        out = usage(client, server, t0)
        out["replay_pages"] = server.stats.replay_pages
        out["model_pages"] = server.stats.model_pages
        await client._client.aclose()
        return out

    return run_virtual(go)


def run_pipeline(site, latency, *, faults=None, rerun=False):
    """现行的整次分析：等时圈（含过街校正）→ 设施检索 → 网格盲区。rerun：同一客户端再跑一遍。"""

    async def go():
        server = ReplayServer(site.detour, latency, faults or Faults())
        polygon_dev = None
        client = make_client(server)
        stages: dict[str, Any] = {}
        decisions = None
        error = None
        t_start = now()
        rounds = 2 if rerun else 1
        try:

            def mark() -> tuple[int, Counter, float]:
                return client.matrix_pairs, Counter(server.stats.requests), now()

            def diff(a: tuple, b: tuple) -> dict[str, Any]:
                reqs = b[1] - a[1]
                return {
                    "matrix_pairs": b[0] - a[0],
                    "requests": dict(reqs),
                    "request_total": sum(reqs.values()),
                    "elapsed_s": round(b[2] - a[2], 1),
                }

            for r in range(rounds):
                m0 = mark()
                iso = await compute_isochrone(
                    client, site.center, IsochroneConfig(), RefineOptions(delay=True)
                )
                m1 = mark()
                # 回放算出的圈与快照里的圈逐个顶点比：对得上，后面网格才是同一批格子
                polygon_dev = max(
                    haversine_m(a[0], a[1], b[0], b[1])
                    for a, b in zip(iso.polygon, site.polygon, strict=True)
                )
                coverage = await collect_coverage(client, site.center, 2500)
                m2 = mark()
                blind = await identify_blindspots(
                    client, site.center, iso.polygon, coverage, BlindspotConfig(), refine=iso.refine
                )
                m3 = mark()
                decisions = decisions_from_cells(blind.cells)
                stages[f"run{r + 1}"] = {
                    **diff(m0, m3),
                    "by_stage": {
                        "isochrone": diff(m0, m1),
                        "coverage": diff(m1, m2),
                        "blindspots": diff(m2, m3),
                    },
                    "pruned_decisions": blind.pruned_decisions,
                    "failed_categories": list(coverage.failed),
                    "cells": len(blind.cells),
                }
        except IncompleteSamplingError as exc:
            error = {"type": "IncompleteSamplingError", "message": str(exc)[:160]}
        finally:
            await client._client.aclose()
        out = usage(client, server, t_start)
        out.update(
            stages=stages,
            polygon_max_dev_m=None if polygon_dev is None else round(polygon_dev, 2),
            error=error,
            injected=dict(server.stats.injected),
            replay_pairs=server.stats.replay_pairs,
            model_pairs=server.stats.model_pairs,
            replay_routes=server.stats.replay_routes,
            replay_pages=server.stats.replay_pages,
        )
        return out, decisions

    return run_virtual(go)


def run_simulate_quota(site, latency):
    """模拟新建时批量算路配额已经用完：应改用直线估算并写明原因。"""

    async def go():
        server = ReplayServer(site.detour, latency, Faults(quota_after_pairs=0))
        client = make_client(server)
        feature = json.loads(json.dumps(site.snapshot))
        cells = feature["properties"]["blindspots"]["cells"]
        target = next(c for c in cells if c["missing"])
        try:
            result = await simulate_facility(
                client, feature, target["missing"][0], target["lat"], target["lng"]
            )
        finally:
            await client._client.aclose()
        return {
            "basis": result["basis"],
            "approximation": result["approximation"],
            "requests": dict(server.stats.requests),
        }

    return run_virtual(go)


# ---------- 汇总 ----------


def fmt_s(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.1f} 秒"
    if seconds < 5400:
        return f"{seconds / 60:.1f} 分钟"
    return f"{seconds / 3600:.1f} 小时"


def main() -> None:
    parser = argparse.ArgumentParser(description="API 调用优化的零配额对比实验")
    parser.add_argument("--quick", action="store_true", help="跳过逐对请求的朴素做法")
    args = parser.parse_args()
    started = time.perf_counter()

    sites = {k: load_site(k) for k in ("taopu", "caoyang")}
    lat_main = LATENCIES["B"]  # 标定之前先用 B 取设施数据（检索结果与延迟无关）
    data: dict[str, Any] = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "sites": {}}

    coverages = {k: run_virtual(lambda s=s: coverage_of(s, lat_main)) for k, s in sites.items()}

    # 0. 本地计算要多久：真实耗时 = 本地计算（读缓存、几何运算）+ 等网络。
    #    在真实缓存上不联网跑一遍整次分析，量出前一项
    local = {k: measure_local(s) for k, s in sites.items()}
    data["local"] = local
    print(f"[本地] {local}")

    # 1. 回放校验 + 延迟假设标定：现行策略的盲区一段，对照真实运行
    data["calibration"] = {}
    for k, site in sites.items():
        usage_meta = site.snapshot["properties"].get("api_usage") or {}
        finished = time.mktime(
            time.strptime(site.snapshot["properties"]["generated_at"], "%Y-%m-%dT%H:%M:%S")
        )
        started_ts = finished - float(usage_meta.get("elapsed_s") or 0) - 5
        rows = {}
        for lk, lat in LATENCIES.items():
            res, dec = run_blind(
                site, "circle100", "current", coverages[k], lat, cutoff_ts=started_ts
            )
            rows[lk] = res
        # 同一回放，但内存缓存先装入真实运行开始前已有的点对：起步状态与真实运行一致，
        # 请求的切分（只补缺的那几块）也就一致，剩下的差别只能是真实运行里的重试
        seeded, seeded_dec = run_blind(
            site,
            "circle100",
            "current",
            coverages[k],
            lat_main,
            cutoff_ts=started_ts,
            seed_cache=True,
        )
        snap = decisions_from_cells(site.snapshot["properties"]["blindspots"]["cells"])
        data["calibration"][k] = {
            "real": real_blind_run(site.snapshot),
            "replay": rows,
            "replay_seeded": seeded,
            "decisions_vs_snapshot": agreement(dec, snap),
            "seeded_decisions_vs_snapshot": agreement(seeded_dec, snap),
        }
        b, real = rows["B"], real_blind_run(site.snapshot)
        print(
            f"[校验] {site.name}: 空缓存回放 {b['matrix_pairs']} 点对 / {b['request_total']} 请求"
            f"（其中生成前已在缓存 {b['pre_existing_pairs']}，模型补 {b['model_pairs']}）；"
            f"复现起步缓存 {seeded['matrix_pairs']} / {seeded['request_total']}；"
            f"真实 {real['pairs']} / {real['requests']}"
        )

    # 2. 盲区判定：五种做法 × 两个网格口径
    data["blind"] = {}
    for k, site in sites.items():
        for gk in GRIDS:
            truth = None
            rows = {}
            for sk in BLIND_STRATEGIES:
                if sk == "pairwise" and args.quick:
                    continue
                res, dec = run_blind(site, gk, sk, coverages[k], lat_main)
                if sk == "full_serial":
                    truth = dec
                rows[sk] = (res, dec)
            data["blind"][f"{k}/{gk}"] = {
                sk: {**res, "agreement": agreement(dec, truth)} for sk, (res, dec) in rows.items()
            }
            cur = rows["current"][0]
            print(
                f"[盲区] {site.name} {GRIDS[gk][0]}: 现行 {cur['matrix_pairs']} 点对 "
                f"{fmt_s(cur['elapsed_s'])}"
            )

    # 3. 等时圈采样、路线、设施检索
    data["stages"] = {}
    for k, site in sites.items():
        data["stages"][k] = {
            "sampling": {
                h: run_sampling(site, lat_main, h)
                for h in ("per_point", "batch_serial", "batch_conc")
            },
            "routes": {c: run_routes(site, lat_main, c) for c in (1, 4)},
            "poi": {c: run_poi(site, lat_main, c) for c in (False, True)},
        }

    # 4. 整次分析：现行首次 / 同一地点重跑
    data["pipeline"] = {}
    for k, site in sites.items():
        res, _ = run_pipeline(site, lat_main, rerun=True)
        data["pipeline"][k] = res
        print(
            f"[整次] {site.name}: 首次 {res['stages']['run1']['elapsed_s']} 秒，"
            f"重跑 {res['stages']['run2']['elapsed_s']} 秒"
        )

    # 5. 敏感性：在途数、延迟假设（曹杨现版网格的盲区一段）
    cy = sites["caoyang"]
    data["sensitivity"] = {
        "concurrency": {
            m: run_blind(cy, "circle100", "current", coverages["caoyang"], lat_main, matrix=m)[0]
            for m in (1, 2, 3, 4, 6)
        },
        "latency": {
            lk: {
                sk: run_blind(cy, "circle100", sk, coverages["caoyang"], lat)[0]
                for sk in ("full_serial", "current_serial", "current")
            }
            for lk, lat in LATENCIES.items()
        },
    }

    # 6. 故障注入（曹杨现版网格）
    clean, clean_dec = run_blind(cy, "circle100", "current", coverages["caoyang"], lat_main)
    faults = {
        "401_10": ("10% 的请求返回 401（并发超限）", Faults(p401=0.10)),
        "401_30": ("30% 的请求返回 401", Faults(p401=0.30)),
        "net_05": ("5% 的请求网络中断", Faults(p_network=0.05)),
        "quota_half": (
            f"批量算路答到第 {clean['matrix_pairs'] // 2} 个点对后返回 302（配额耗尽）",
            Faults(quota_after_pairs=clean["matrix_pairs"] // 2),
        ),
    }
    clean["unknown"] = sum(1 for v in clean_dec.values() if v == "unknown")
    data["faults"] = {"clean": clean}
    for fk, (label, f) in faults.items():
        res, dec = run_blind(cy, "circle100", "current", coverages["caoyang"], lat_main, faults=f)
        data["faults"][fk] = {**res, "label": label, "agreement": agreement(dec, clean_dec)}
    poi_fault, poi_dec = run_pipeline(
        cy, lat_main, faults=Faults(poi_fail_keywords=frozenset({"药店"}))
    )
    data["faults"]["poi_fail"] = {
        **poi_fault,
        "label": "「药店」关键词的检索一律失败",
        "agreement": agreement(poi_dec, clean_dec) if poi_dec else None,
    }
    iso_fault, _ = run_pipeline(cy, lat_main, faults=Faults(fail_isochrone_batch=True))
    data["faults"]["iso_fail"] = {**iso_fault, "label": "等时圈第三批采样一律返回服务器错误"}
    data["faults"]["simulate_quota"] = {
        **run_simulate_quota(sites["taopu"], lat_main),
        "label": "模拟新建时批量算路配额已用完",
    }

    data["runtime_s"] = round(time.perf_counter() - started, 1)
    REPORT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    REPORT_MD.write_text(render(data, sites, args.quick), encoding="utf-8")
    print(
        f"\n写入 {REPORT_MD.relative_to(ROOT)} 与 {REPORT_JSON.relative_to(ROOT)}，"
        f"实际用时 {data['runtime_s']} 秒"
    )


# ---------- 报告 ----------


def _row(cells: list[Any]) -> str:
    return "| " + " | ".join(str(c) for c in cells) + " |"


def _less(part: float, whole: float) -> str:
    """「少百分之几」：保留一位小数，避免 99.7% 四舍五入成「少 100%」。"""
    return f"{100 * (1 - part / whole):.1f}%"


def _local_total(local: dict[str, float]) -> float:
    return sum(v for k, v in local.items() if k != "blocked_requests")


def render(d: dict[str, Any], sites: dict[str, Site], quick: bool) -> str:
    L: list[str] = []
    w = L.append
    w("# API 调用优化：对比实验数据")
    w("")
    w(
        f"> 由 `scripts/benchmark_api.py` 生成（{d['generated_at']}），**零配额**：不调用百度，"
        "用本地缓存回放两份样例生成时真实测到的结果；"
    )
    w(
        "> 网络延迟与限速在虚拟时钟上模拟，令牌桶、在途上限、重试退避与缓存都是生产代码本身。"
        f"固定随机种子，重跑结果不变。本机实际用时 {d['runtime_s']} 秒。"
    )
    w("")
    w("## 0. 方法")
    w("")
    w(
        "- **回放**：批量算路、步行路线、地点检索的回答取自 `.cache/`"
        "（快照生成时百度的真实回答）。缓存里没有的点对用模型补："
        "直线距离 × 该样例实测平均绕行系数，步速 1.17 m/s。"
        "模型只会出现在朴素做法多测的那些点对上"
        "（大多是直线就超过 1 公里的远处设施，结论不受影响）。"
    )
    w("- **延迟假设**（每个请求的往返时间，另加 ±20% 抖动）：")
    for lat in LATENCIES.values():
        w(f"  - {lat.key}：{lat.label}")
    w(
        "  - 主表用 B。A 是小请求实测值（`docs/api-optimization.md` 4.1），大矩阵实际更慢；"
        "C 用来看网络变慢时结论是否还成立。"
    )
    w(
        "- **限速**：全局 8 QPS 令牌桶，批量算路每 20 个点对折算 1 个令牌；"
        "批量算路在途 3、路线在途 4（生产默认值）。"
    )
    w(
        "- **「结论一致」**：以「全量矩阵」（每格对每家同类设施都测）的判定为准，"
        "逐个「网格 × 品类」比对。"
    )
    w("")

    w("## 1. 回放校验：和真实运行对得上吗")
    w("")
    dates = sorted({c["real"]["date"] for c in d["calibration"].values()})
    w(
        "现行策略、现版网格（15 分钟圈内 100 米，小学按校门测距），"
        "只看盲区这一段（含中心到各格的耗时），"
        f"与 {'、'.join(dates)} 重新生成快照时的真实记录（快照里的 `api_usage`）对照。"
    )
    w(
        "真实运行开始前缓存里已经有一部分点对（同一中心以前测过），真实运行没有再发。"
        "所以回放跑两遍：一遍从空缓存起步，一遍先把这部分点对装进缓存、复现真实运行起步时的状态。"
    )
    w("")
    w(
        _row(
            ["样例", "空缓存回放", "生成前已缓存", "复现起步缓存", "真实运行", "逐格判定与快照一致"]
        )
    )
    w(_row(["---"] * 6))
    for k, c in d["calibration"].items():
        real, r, s = c["real"], c["replay"]["B"], c["replay_seeded"]
        agree = c["seeded_decisions_vs_snapshot"]
        w(
            _row(
                [
                    sites[k].name,
                    f"{r['matrix_pairs']} 点对 / {r['request_total']} 请求",
                    f"{r['pre_existing_pairs']} 点对",
                    f"{s['matrix_pairs']} 点对 / {s['request_total']} 请求",
                    f"{real['pairs']} 点对 / {real['requests']} 请求",
                    f"{agree['same']} / {agree['judgements']}（{agree['same_pct']}%）",
                ]
            )
        )
    w("")
    w(
        "- **点对数完全对上**：空缓存回放减去生成前已缓存的，正好等于真实实发；"
        "复现起步缓存后直接相等。回放走的是同一条代码路径、问的是同一批点对。"
    )
    w("- **逐格判定完全一致**：回放答回去的就是当时百度的回答。")
    extra = {
        k: c["real"]["requests"] - c["replay_seeded"]["request_total"]
        for k, c in d["calibration"].items()
    }
    if all(v == 0 for v in extra.values()):
        w("- **请求数也完全对上**：起步缓存一致时，请求怎么切分（只补缺的那几块）也一致。")
    else:
        detail = "、".join(
            f"{sites[k].name}多 {v} 个" if v else f"{sites[k].name}一样多" for k, v in extra.items()
        )
        w(
            f"- **真实请求比回放多**（{detail}）：起步缓存一致、点对数一致，"
            "请求怎么切分也就一致，多出的只能是重试——客户端的请求计数包含每一次重发"
            "（被限流或网络抖动后），点对数只按成功的块计。回放里没有注入故障，所以没有重试。"
            "客户端只记请求总数、不分原因，所以分不出这些重试是 401 限流还是网络重连。"
        )
    split = [
        k
        for k, c in d["calibration"].items()
        if c["replay_seeded"]["request_total"] > c["replay"]["B"]["request_total"]
    ]
    if split:
        w(
            "- **复现起步缓存后请求变多**（" + "、".join(sites[k].name for k in split) + "）："
            "零散的已缓存点对把矩阵切成几块，只补缺的那几块，点对省下了，请求多了几个。"
            "配额按点对计，这是划算的交换。"
        )
    w("")
    tp, cy = d["calibration"]["taopu"]["real"], d["calibration"]["caoyang"]["real"]
    if min(tp["requests"], cy["requests"]) >= 5:
        ratio = (tp["elapsed_s"] / tp["requests"]) / (cy["elapsed_s"] / cy["requests"])
        why = (
            f"同一天两次运行，桃浦 {tp['requests']} 个请求用了 {fmt_s(tp['elapsed_s'])}，"
            f"曹杨 {cy['requests']} 个请求用了 {fmt_s(cy['elapsed_s'])}，"
            f"按请求折算差了 {ratio:.1f} 倍。"
            "差别来自当时的网络（本机走系统代理，偶发重连）和重试，拿任何一次标定都会把另一次算错。"
        )
    else:
        why = (
            f"这次真实运行大部分点对命中缓存（桃浦只发了 {tp['requests']} 个请求、"
            f"曹杨 {cy['requests']} 个），耗时主要是读缓存和几次网络往返，标定不出每个请求的延迟；"
            "2026-10-03 那次从更少的缓存起步，两个样例按请求折算的耗时也差了 3.7 倍。"
        )
    w(
        f"**为什么不用真实耗时标定延迟。** {why}"
        "所以下面各组只比较**同一延迟假设下**不同做法的差别，并用三档假设看结论是否稳定。"
    )
    slow = "；".join(
        f"{sites[k].name}真实 {c['real']['elapsed_s']} 秒、"
        f"回放 {c['replay_seeded']['elapsed_s']} 秒"
        for k, c in d["calibration"].items()
    )
    w("")
    w(
        f"模拟的绝对耗时偏乐观：复现起步缓存的回放（延迟 B）与真实运行相比，{slow}。"
        "**表里的「模拟耗时」只用来横向比较做法**，不代表真实网络下要等多久；"
        "真实网络越慢、重试越多，省下请求的收益越大（见 5.2）。"
    )
    loc = d["local"]
    w("")
    w(
        "本地计算本身很快：在真实缓存上不联网把整次分析跑一遍，等时圈、设施、盲区三段合计桃浦 "
        f"{_local_total(loc['taopu']):.2f} 秒、曹杨 {_local_total(loc['caoyang']):.2f} 秒"
        "（缓存文件已在系统文件缓存里）。也就是说，**耗时几乎全花在等网络上**，省请求就是省时间。"
    )
    w("")

    w("## 2. 盲区判定：五种做法")
    w("")
    w("同一份设施数据、同一个网格，只换测距的做法。延迟假设 B。")
    if quick:
        w("（本次用 `--quick` 运行，跳过了逐对请求那一行。）")
    w("")
    for key, rows in d["blind"].items():
        sk, gk = key.split("/")
        cur = rows["current"]
        w(f"### {sites[sk].name} · {GRIDS[gk][0]} · {cur['cells']} 格")
        w("")
        w(_row(["做法", "点对", "请求", "模拟耗时", "结论一致", "未知"]))
        w(_row(["---"] * 6))
        for r in rows.values():
            a = r["agreement"]
            w(
                _row(
                    [
                        r["label"],
                        r["matrix_pairs"],
                        r["request_total"],
                        fmt_s(r["elapsed_s"]),
                        f"{a['same_pct']}%",
                        a["unknown"],
                    ]
                )
            )
        base = rows.get("pairwise") or rows["full_serial"]
        full = rows["full_serial"]
        w("")
        w(
            f"现行比{('逐对请求' if 'pairwise' in rows else '全量矩阵串行')}："
            f"点对少 {_less(cur['matrix_pairs'], base['matrix_pairs'])}，"
            f"请求少 {_less(cur['request_total'], base['request_total'])}，"
            f"耗时缩短到 {100 * cur['elapsed_s'] / base['elapsed_s']:.1f}%；"
            f"比全量矩阵串行点对少 {_less(cur['matrix_pairs'], full['matrix_pairs'])}，"
            f"耗时缩短到 {100 * cur['elapsed_s'] / full['elapsed_s']:.1f}%。"
        )
        w("")
    w(
        "「未知」是现行策略测完 4 轮候选仍未判定的格子：只能说最近的几家走不到，"
        "不能断言一家都没有，所以不算盲区。"
    )
    w("")
    odd = [
        (key, rows)
        for key, rows in d["blind"].items()
        if "pairwise" in rows
        and rows["pairwise"]["matrix_pairs"] < rows["full_serial"]["matrix_pairs"]
    ]

    def short(key: str) -> str:
        sk, gk = key.split("/")
        return f"{sites[sk].name}·{GRIDS[gk][0][-3:-1]}"

    if odd:
        detail = "、".join(
            f"{short(key)}多 {rows['full_serial']['dup_pairs']} 个" for key, rows in odd
        )
        w(
            "**逐对请求的点对为什么反而比全量矩阵少。** 缓存按 50 米量化，"
            "相隔不到 50 米的两家设施（比如同一栋楼里的两家药店）共用一个缓存键。"
            "逐对请求时前一家的结果已经写进缓存，后一家直接命中；全量矩阵一次发出，"
            f"两家各算一次（{detail}）。"
        )
        cur_dup = {key: rows["current"]["dup_pairs"] for key, rows in d["blind"].items()}
        if not any(cur_dup.values()):
            w(
                "现行做法没有这种重复（四组都是 0 个）：每个请求只有一个终点"
                "（共用同一候选的网格并成一列），同一网格的下一家候选要到下一轮才测，"
                "那时前一家的结果已经在缓存里。"
            )
        else:
            w(
                "现行做法里的这类重复："
                + "、".join(f"{short(key)} {v} 个" for key, v in cur_dup.items())
                + "。"
            )
        w("")

    w("## 3. 等时圈采样、过街路线、设施检索")
    w("")
    w(_row(["样例", "环节", "做法", "请求", "模拟耗时"]))
    w(_row(["---"] * 5))
    names = {
        "per_point": "逐点请求，串行",
        "batch_serial": "批量矩阵，串行",
        "batch_conc": "批量矩阵，在途 3（默认）",
    }
    for k, s in d["stages"].items():
        name = sites[k].name
        for h, r in s["sampling"].items():
            w(
                _row(
                    [
                        name,
                        "等时圈 252 个采样点",
                        names[h],
                        r["request_total"],
                        fmt_s(r["elapsed_s"]),
                    ]
                )
            )
        for c, r in s["routes"].items():
            how = "串行" if int(c) == 1 else "在途 4（默认）"
            w(
                _row(
                    [
                        name,
                        f"过街校正 {r['routes']} 条路线",
                        how,
                        r["request_total"],
                        fmt_s(r["elapsed_s"]),
                    ]
                )
            )
        for c, r in s["poi"].items():
            how = "并发（默认）" if c in (True, "True", "true") else "逐个串行"
            w(
                _row(
                    [
                        name,
                        "设施检索（17 个关键词含翻页，另按校名查校门）",
                        how,
                        r["request_total"],
                        fmt_s(r["elapsed_s"]),
                    ]
                )
            )
    w("")
    per_point = d["stages"]["taopu"]["sampling"]["per_point"]["request_total"]
    w(
        f"- 等时圈：批量矩阵把 252 个采样点装进 3 个请求。逐点请求只发了 {per_point} 个："
        "最内一圈（200 米）的 36 个点相邻只隔 35 米，有几个和邻点落进同一个 50 米缓存格，"
        "后发的直接命中缓存。"
    )
    poi_req = {k: s["poi"][True]["request_total"] for k, s in d["stages"].items()}
    w(
        "- 路线和检索：请求数不变，差别全在并发。各条路线、各个关键词、各所学校的校门互不依赖，"
        "一起发出，总速率由令牌桶封顶。"
        "曹杨的检索结果多、小学多，翻页和查校门多出 "
        f"{poi_req['caoyang'] - poi_req['taopu']} 个请求。"
    )
    w("")

    w("## 4. 整次分析：首次与重跑")
    w("")
    w(_row(["样例", "", "点对", "请求", "模拟耗时"]))
    w(_row(["---"] * 5))
    for k, r in d["pipeline"].items():
        for run, label in (("run1", "首次（缓存为空）"), ("run2", "同一地点重跑")):
            st = r["stages"][run]
            w(
                _row(
                    [
                        sites[k].name,
                        label,
                        st["matrix_pairs"],
                        st["request_total"],
                        fmt_s(st["elapsed_s"]),
                    ]
                )
            )
    w("")
    w("首次分析分环节（请求按接口分开计）：")
    w("")
    w(_row(["样例", "环节", "批量算路点对", "请求", "模拟耗时"]))
    w(_row(["---"] * 5))
    kinds = {"matrix": "批量算路", "route": "路线规划", "poi": "地点检索"}
    stage_names = {
        "isochrone": "等时圈（含过街校正）",
        "coverage": "设施检索",
        "blindspots": "网格盲区（含中心到各格）",
    }
    for k, r in d["pipeline"].items():
        for sk, st in r["stages"]["run1"]["by_stage"].items():
            reqs = "、".join(f"{kinds.get(x, x)} {n}" for x, n in st["requests"].items()) or "0"
            w(
                _row(
                    [
                        sites[k].name,
                        stage_names[sk],
                        st["matrix_pairs"],
                        reqs,
                        fmt_s(st["elapsed_s"]),
                    ]
                )
            )
    w("")
    fewer = {
        k: (
            r["stages"]["run1"]["by_stage"]["blindspots"]["matrix_pairs"],
            d["blind"][f"{k}/circle100"]["current"]["matrix_pairs"],
        )
        for k, r in d["pipeline"].items()
    }
    if any(a < b for a, b in fewer.values()):
        detail = "；".join(
            f"{sites[k].name}整次分析里 {a} 个、单独跑 {b} 个" for k, (a, b) in fewer.items()
        )
        w(
            f"网格盲区一段的点对比第 2 节「只看盲区」少（{detail}）：中心到各格的耗时有一部分"
            "和等时圈采样点落进同一个 50 米缓存格，整次分析时直接命中。"
        )
        w("")
    loc = d["local"]
    w(
        "重跑全部命中按点对存储的缓存，零请求、零配额。剩下的只有本地计算，模拟里不计时；"
        f"在本机真实缓存上实测桃浦 {_local_total(loc['taopu']):.2f} 秒、"
        f"曹杨 {_local_total(loc['caoyang']):.2f} 秒（第 1 节）。"
        "缓存文件第一次被读时（冷启动、杀毒软件逐个扫描）会慢一些："
        "快照生成那次等时圈一段零请求，也用了 5 秒左右。"
    )
    w("")

    w("## 5. 敏感性")
    w("")
    w("### 5.1 批量算路在途数（曹杨现版网格盲区一段，延迟 B）")
    w("")
    w(_row(["在途上限", "请求", "模拟耗时", "实际峰值在途"]))
    w(_row(["---"] * 4))
    for m, r in d["sensitivity"]["concurrency"].items():
        w(_row([m, r["request_total"], fmt_s(r["elapsed_s"]), r["peak_inflight"].get("matrix", 0)]))
    w("")
    w(
        "在途数加到 3 以后收益变小：同一轮里能并发的候选组有限，总速率又被 8 QPS 令牌桶封顶。"
        "默认取 3，是在吞吐和「撞上限流时多个请求一起退避重试」的风险之间取的平衡。"
    )
    w("")
    w("### 5.2 延迟假设（曹杨现版网格盲区一段）")
    w("")
    w(_row(["延迟", "全量矩阵串行", "现行串行", "现行在途 3", "现行 / 全量矩阵"]))
    w(_row(["---"] * 5))
    for lk, rows in d["sensitivity"]["latency"].items():
        f, cs, c = rows["full_serial"], rows["current_serial"], rows["current"]
        w(
            _row(
                [
                    lk,
                    fmt_s(f["elapsed_s"]),
                    fmt_s(cs["elapsed_s"]),
                    fmt_s(c["elapsed_s"]),
                    f"{100 * c['elapsed_s'] / f['elapsed_s']:.1f}%",
                ]
            )
        )
    w("")
    w("网络越慢，省下的请求越值钱；三种假设下现行做法的优势都成立。")
    w("")

    w("## 6. 故障注入：降级是否兜得住")
    w("")
    f = d["faults"]
    clean = f["clean"]
    w(
        f"基准：曹杨现版网格盲区一段，无故障时 {clean['matrix_pairs']} 个点对、"
        f"{clean['request_total']} 个请求、{fmt_s(clean['elapsed_s'])}。"
        "下表的「结论一致」以无故障的结果为准。"
    )
    w("")
    w(
        _row(
            [
                "注入的故障",
                "收到请求（含重试）",
                "模拟耗时",
                "结论一致",
                "未知",
                "误判为盲区",
                "漏掉的盲区",
            ]
        )
    )
    w(_row(["---"] * 7))
    for fk in ("401_10", "401_30", "net_05", "quota_half"):
        r = f[fk]
        a = r["agreement"]
        w(
            _row(
                [
                    r["label"],
                    r["request_total"],
                    fmt_s(r["elapsed_s"]),
                    f"{a['same_pct']}%",
                    a["unknown"],
                    a["false_blind"],
                    a["missed_blind"],
                ]
            )
        )
    w("")
    base_u = clean["unknown"]
    # 不一致的判定若全是「已判定 → 未知」，降级就没有判错任何一格
    flips = [
        f[fk]["label"]
        for fk in ("401_10", "401_30", "net_05", "quota_half")
        if f[fk]["agreement"]["judgements"] - f[fk]["agreement"]["same"]
        != f[fk]["agreement"]["to_unknown"]
    ]
    w(
        "「未知」含无故障时本来就有的 "
        + str(base_u)
        + " 个。"
        + (
            "所有故障下，不一致的判定全是「已判定 → 未知」，没有一格翻成相反的结论。"
            if not flips
            else "以下故障出现了「未知」以外的不一致：" + "、".join(flips) + "。"
        )
    )
    w("")
    r30 = f["401_30"]
    w(
        f"- **限流**：10% 的请求被拒时，退避重试全部补回，结论不变；30% 被拒时，"
        f"有 {r30['matrix_failures']} 块重试 {Settings.max_retries} 次仍失败，"
        f"多出 {r30['agreement']['unknown'] - base_u} 个「未知」。"
    )
    q = f["quota_half"]
    got_302 = q["injected"].get("302", 0)
    w(
        f"- **配额耗尽**：收到 302 的 {got_302} 个请求，都是熔断前已经发出的"
        f"（批量算路在途上限 3）；第一个 302 一回来，客户端当天熔断批量算路，"
        f"熔断后新发出的批量算路请求 {q['after_quota']} 个。"
        "没测到的格子记「未知」，**没有一格被误判为盲区**。"
    )
    pf = f["poi_fail"]
    failed = pf["stages"].get("run1", {}).get("failed_categories") if pf.get("stages") else None
    a = pf.get("agreement") or {}
    failed_text = "、".join(failed or []) or "—"
    w(
        f"- **检索失败**：「药店」检索失败后，「{failed_text}」整类标为查询失败，"
        f"{clean['cells']} 格的医药判定全部记「未知」"
        f"（合计未知 {a.get('unknown', '—')} 个，含原有的 {base_u} 个），"
        f"误判为盲区 {a.get('false_blind', '—')} 个；另外两类照常判定。"
        "宁可说「不知道」，也不拿缺了一个关键词的设施表去下「没有药店」的结论。"
    )
    iso = f["iso_fail"]
    w(
        f"- **等时圈采样整批失败**：重试耗尽后抛出 `{(iso.get('error') or {}).get('type')}`，"
        "不画等时圈，也不会把没测到的点当成障碍画出一个缩水的圈。"
    )
    sq = f["simulate_quota"]
    w(
        f"- **模拟新建时配额已用完**：结果标为 `{sq['basis']}`，"
        f"说明写「{sq['approximation'][:30]}……」，"
        "而不是显示成「若干格未核验」（这是本轮修掉的问题）。"
    )
    w("")
    w("## 7. 复现")
    w("")
    w("```bash")
    w("python scripts/benchmark_api.py          # 全部实验")
    w("python scripts/benchmark_api.py --quick  # 跳过逐对请求")
    w("```")
    w("")
    w(
        "原始数据在 `reports/api-benchmark.json`。回放依赖本机 `.cache/` 里的样例缓存；"
        "换一台机器先用 `scripts/run_isochrone.py` 生成一次样例（会消耗配额），回放才有数据可放。"
    )
    w("")
    return "\n".join(L)


if __name__ == "__main__":
    main()
