"""网格交叉校核：用另一组网格样本核对射线等时圈。

射线圈（36 方向 × 7 档半径，射线之间直线相连）仍是结果的主几何：面积、圈内设施、盲区占比与评分
都按它算。这里只做核对，输出一个辅助图层，回答三个问题：

1. **射线圈里有没有走不到的地方（缺口）？** 盲区判定本来就测了「中心 → 每个格心」的步行
   耗时（默认 100 米格；方格边长都按实际网格间距 s 算：s、s/2、s/4）。射线圈内耗时超过 900 秒的格，
   再在格内测 4 个子点；子点之间仍有分界时，再把超时的子格切成 4 个更细的点。
   **只有确认超过 900 秒的点才画成缺口**：不计过街等待就已超时，或用这个点自己的步行路线补上
   等待后超时。只靠从射线插值来的等待才超时的点记为「待核实」，不画。子点结论不一致时保留差异，
   不按多数票整格删除。取过自己路线的点，路线规划自己给的耗时也要超过 900 秒：它与「批量算路
   距离 + 路线剖面等待」分处 900 秒两侧时记为「存疑」，不画。一个点都没取过路线的片，再取批量算路
   最远的一点单独核验路线；路线说 15 分钟内走得到，整片记为存疑。
2. **哪里有绕行线索？** 相邻两格相距 s 米，中心到它们的路网距离为 D(A) > D(B)。由三角不等式，
   从 B 走到 A 至少要 D(A) − D(B) 米（同一接口、同一端点口径下成立；两端吸附到不同的路上时不成立）。
   正常街区里相邻格之间的步行距离大致不超过 2·s，故 D(A) − D(B) > κ·s + 2ε 记为一条线索
   （κ = 2、ε = 30 米，都是经验参数，不是已证明的误差上限）。相连的线索串成一段；
   最大跳变 ≥ 400 米（经验筛选阈值）的段才显示，并直接测一次 **B → A**（不假设双向对称）：
   实测也超过 κ·s + 2ε 的记「已实测」，没超过的说明两次测距吸附到了不同的路上，记「存疑」不显示。
   线索的位置只是两格的公共边，不是围墙或河道的真实位置。
3. **校核估计面积是多少？** 射线采样点、格心、外圈格与子点一起做受约束的局部线性插值
   （isochrone/tin.py），取 900 秒等值线。它是另一种插值估计，不替代射线圈。

**口径说明（不是独立校核）。** 方格的路网距离与不含等待的耗时是逐点独立测得的；但方格的过街等待
仍取自相邻射线的路线剖面（插值），只有单独取过路线的点才用自己的等待。所以这是「另一组网格样本
交叉校核」：能发现射线之间漏掉的缺口，却不能独立检验射线自己的过街等待。

所有「中心 → 点」的测距都按 10 米细键缓存，子点不会拿到父格的结果；细键只解决缓存混用，
解决不了接口把点吸附到哪条路上。新点没有缓存时会花批量算路点对，受 max_pairs 预算约束。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any

from ..baidu.errors import BaiduApiError, QuotaExhaustedError
from .geometry import (
    METERS_PER_DEG_LAT,
    haversine_m,
    meters_per_deg_lng,
    point_in_polygon,
    simplify_path,
)
from .refine import RefineResult, build_profile
from .tin import Vertex, areas_m2, classify_rings, contour_rings, interpolate, ring_area

POINT_GRID_M = 10.0  # 「中心 → 点」的终点细键；起点是同一个中心
# 显示的绕行线索：已实测（confirmed）与预算没测到（unverified）。存疑的只计数
SHOWN = ("confirmed", "unverified")
ROUTE_OFF_NETWORK_M = 50.0  # 路线终点离目标点超过这么远，这个点可能不在路网上


@dataclass(frozen=True)
class GridcheckConfig:
    target_s: float = 900.0
    spacing_m: float = 100.0
    kappa: float = 2.0
    eps_m: float = 30.0
    # 绕行线索的显示阈值（米，段内最大跳变）：经验筛选，260~400 米的边在样例里有误报
    clue_show_m: float = 400.0
    max_clues: int = 8
    max_pairs: int = 400
    max_routes: int = 40
    outer_rounds: int = 2
    max_quarter_pairs: int = 40
    raster_m: float = 10.0
    max_edge_m: float = 300.0

    def clue_threshold(self, s: float) -> float:
        return self.kappa * s + 2 * self.eps_m


@dataclass
class Probe:
    """一个测时点：盲区格心（level 0，边长为网格间距 s）、子点（1，s/2）或更细一层（2，s/4）。"""

    key: tuple[int, int]  # 以 1/8 格距为单位的格网坐标 (Y, X)
    level: int
    lat: float
    lng: float
    kind: str  # cell：射线圈内的盲区格；outer：外圈格；sub：子点
    inside: bool  # 点是否在射线圈内
    spacing_m: float = 100.0  # 盲区网格间距：level 0 方格的边长
    raw_s: float | None = None  # 批量算路耗时，不含过街等待
    dist_m: float | None = None  # 中心到该点的路网距离
    delay_s: float = 0.0
    delay_source: str = "none"  # ray_interp / own_route / none
    blocked: bool = False  # 路线穿过施工围挡
    measured: bool = False
    off_network: bool = False
    route_checked: bool = False
    # 单独取过的步行路线自己给的耗时与距离（含过街等待）。eff_s 仍是「批量算路 + 等待」，
    # 两者分处目标两侧时结论存疑
    route_s: float | None = None
    route_m: float | None = None
    # 一片缺口里被选出来代表整片核验路线的点
    representative: bool = False
    stored_raw_s: float | None = None  # 结果里原有的值（旧结果按 50 米量化测得）
    children: list[tuple[int, int]] = field(default_factory=list)

    @property
    def size_m(self) -> float:
        return self.spacing_m / (2**self.level)

    @property
    def eff_s(self) -> float | None:
        if self.raw_s is None:
            return None
        if self.blocked:
            return math.inf
        return self.raw_s + self.delay_s

    def status(self, target: float) -> str:
        """over：确认超时；over_unverified：只靠插值等待才超时；reach；unknown；blocked；
        disputed：取过自己的路线，路线耗时与「批量算路 + 等待」分处目标两侧。"""
        if self.raw_s is None:
            return "unknown"
        if self.blocked:
            return "blocked"
        eff = self.raw_s + self.delay_s
        if self.route_s is not None and (eff > target) != (self.route_s > target):
            # 两种口径打架：批量算路的距离配上路线剖面的等待说超时、路线自己说走得到（或反过来）。
            # 批量算路与路线规划可能吸附到不同的路上，谁对谁错这里判断不了，不下结论
            return "disputed"
        if self.raw_s > target:
            return "over"  # 不计等待已超时，等待只会更长
        if eff <= target:
            return "reach"
        return "over" if self.delay_source == "own_route" else "over_unverified"


class _Lattice:
    """把 100 米格心映射到整数格网，并能生成同一格网上的新点（外圈、子点）。"""

    def __init__(self, cells: list[dict[str, Any]], spacing: float) -> None:
        lats = [float(c["lat"]) for c in cells]
        lngs = [float(c["lng"]) for c in cells]
        self.lat0, self.lng0 = min(lats), min(lngs)
        self.dlat = spacing / METERS_PER_DEG_LAT
        approx = spacing / meters_per_deg_lng(sum(lats) / len(lats))
        ix = [round((g - self.lng0) / approx) for g in lngs]
        num = sum((g - self.lng0) * i for g, i in zip(lngs, ix, strict=True))
        den = sum(i * i for i in ix)
        # grid_points 按固定步长累加经度，按实际坐标回归出步长，新点才和老格心对齐
        self.dlng = num / den if den else approx

    def index(self, lat: float, lng: float) -> tuple[int, int]:
        return round((lat - self.lat0) / self.dlat), round((lng - self.lng0) / self.dlng)

    def at(self, y8: int, x8: int) -> tuple[float, float]:
        return self.lat0 + y8 * self.dlat / 8, self.lng0 + x8 * self.dlng / 8


@dataclass
class _Budget:
    pairs: int
    routes: int
    pairs_used: int = 0
    routes_used: int = 0
    routes_ok: int = 0
    exhausted: str | None = None  # 停下的原因


class _Checker:
    def __init__(
        self,
        client: Any,
        center: tuple[float, float],
        polygon: list[tuple[float, float]],
        refine: RefineResult | None,
        cfg: GridcheckConfig,
    ) -> None:
        self.client = client
        self.center = center
        self.polygon = polygon
        self.refine = refine
        self.cfg = cfg
        self.budget = _Budget(cfg.max_pairs, cfg.max_routes)
        self.probes: dict[tuple[int, int], Probe] = {}
        self.mpl = meters_per_deg_lng(center[0])

    # ---------- 测量 ----------

    def _pairs_now(self) -> int | None:
        return getattr(self.client, "matrix_pairs", None)

    def _is_missing(self, probe: Probe) -> bool:
        probe_cache = getattr(self.client, "missing_pairs", None)
        if probe_cache is None:
            return True
        return (
            probe_cache("walk", [self.center], [(probe.lat, probe.lng)], dest_grid_m=POINT_GRID_M)
            > 0
        )

    async def measure(self, probes: list[Probe], cap: int | None = None) -> list[Probe]:
        """按给定顺序测「中心 → 点」，命中缓存的不占预算。返回实际测到（或已测过）的点。"""
        todo = [p for p in probes if not p.measured]
        if not todo or self.budget.exhausted:
            return [p for p in probes if p.measured]
        room = self.budget.pairs - self.budget.pairs_used
        if cap is not None:
            room = min(room, cap)
        chosen: list[Probe] = []
        spend = 0
        for p in todo:
            if self._is_missing(p):
                if spend >= room:
                    continue
                spend += 1
            chosen.append(p)
        if len(chosen) < len(todo) and not self.budget.exhausted and cap is None:
            self.budget.exhausted = "budget"
        if not chosen:
            return [p for p in probes if p.measured]
        before = self._pairs_now()
        try:
            rows = await self.client.walking_matrix_grid(
                [self.center], [(p.lat, p.lng) for p in chosen], dest_grid_m=POINT_GRID_M
            )
            row = rows[0] if rows else [None] * len(chosen)
        except QuotaExhaustedError:
            self.budget.exhausted = "quota"
            row = [None] * len(chosen)
        except BaiduApiError:
            self.budget.exhausted = "error"
            row = [None] * len(chosen)
        after = self._pairs_now()
        self.budget.pairs_used += (after - before) if before is not None else spend
        for p, entry in zip(chosen, row, strict=True):
            p.measured = True
            if entry is None:
                continue
            p.raw_s = float(entry["duration_s"])
            p.dist_m = float(entry["distance_m"])
            self._interp_delay(p)
        return [p for p in probes if p.measured]

    def _interp_delay(self, p: Probe) -> None:
        if self.refine is None or p.dist_m is None:
            return
        delay, blocked, usable = self.refine.adjust_point(self.center, p.lat, p.lng, p.dist_m)
        p.delay_s = delay
        p.blocked = blocked
        p.delay_source = "ray_interp" if usable and self.refine.delay_applied else "none"

    async def verify_by_route(self, p: Probe) -> None:
        """取这个点自己的步行路线，用它的过街等待替换插值来的等待。"""
        if self.budget.routes_used >= self.budget.routes or p.dist_m is None:
            return
        self.budget.routes_used += 1
        p.route_checked = True
        route = await self.client.walking_route(self.center, (p.lat, p.lng), fine_m=POINT_GRID_M)
        if route is None:
            return
        self.budget.routes_ok += 1
        base = self.refine.base_speed if self.refine is not None else 1.17
        closures = self.refine.closures if self.refine is not None else ()
        profile = build_profile(route, base, closures)
        p.delay_s = profile.delay_at(p.dist_m)
        p.delay_source = "own_route"
        # 路线自己的耗时也留着：status() 拿它与「批量算路距离 + 剖面等待」对照，分处两侧就存疑
        if route.get("duration_s") is not None:
            p.route_s = float(route["duration_s"])
        if route.get("distance_m") is not None:
            p.route_m = float(route["distance_m"])
        p.blocked = profile.blocked_at_m is not None and p.dist_m >= profile.blocked_at_m
        pts = [pt for step in route.get("steps") or [] for pt in step.get("path") or []]
        if pts:
            end = (float(pts[-1][0]), float(pts[-1][1]))
            p.off_network = haversine_m(p.lat, p.lng, *end) > ROUTE_OFF_NETWORK_M

    # ---------- 点的生成 ----------

    def add(self, key, level, kind, lat=None, lng=None, lattice=None) -> Probe | None:
        if key in self.probes:
            return None
        if lat is None or lng is None:
            lat, lng = lattice.at(*key)
        probe = Probe(
            key=key,
            level=level,
            lat=lat,
            lng=lng,
            kind=kind,
            inside=point_in_polygon(lat, lng, self.polygon),
            spacing_m=self.cfg.spacing_m,
        )
        self.probes[key] = probe
        return probe

    def split(self, parent: Probe, lattice: _Lattice) -> list[Probe]:
        step = 2 if parent.level == 0 else 1
        out = []
        for dy in (step, -step):
            for dx in (-step, step):
                key = (parent.key[0] + dy, parent.key[1] + dx)
                child = self.add(key, parent.level + 1, "sub", lattice=lattice)
                if child is not None:
                    parent.children.append(key)
                    out.append(child)
        return out

    def statuses_at(self, key: tuple[int, int]) -> set[str]:
        """50 米子点位置 key 上的结论：有这个子点就看它（切过就看它的 25 米点），
        没有就看覆盖它的 100 米格。"""
        target = self.cfg.target_s
        sub = self.probes.get(key)
        if sub is not None and sub.measured:
            kids = [self.probes[k] for k in sub.children if k in self.probes]
            return {k.status(target) for k in kids} if kids else {sub.status(target)}
        cell = self.probes.get(root_key(key))
        if cell is not None and cell.measured:
            return {cell.status(target)}
        return set()

    def xy(self, lat: float, lng: float) -> tuple[float, float]:
        return (lng - self.center[1]) * self.mpl, (lat - self.center[0]) * METERS_PER_DEG_LAT


def root_key(key: tuple[int, int]) -> tuple[int, int]:
    """点所属 100 米格的格网坐标。子点在格心 ±2、±3 单位处，四舍五入回到 8 的倍数。"""
    return round(key[0] / 8) * 8, round(key[1] / 8) * 8


def ray_samples_of(isochrone: Any) -> list[tuple[float, float, float | None, float | None]]:
    """射线采样点 (纬度, 经度, 有效耗时, 路网距离)。受围挡阻断记 inf，测距失败记 None。"""
    out = []
    for ray in getattr(isochrone, "rays", []) or []:
        for s in ray.samples:
            if s.duration_s is None:
                out.append((s.lat, s.lng, None, None))
            elif s.blocked:
                out.append((s.lat, s.lng, math.inf, s.distance_m))
            else:
                out.append((s.lat, s.lng, s.duration_s + s.delay_s, s.distance_m))
    return out


async def cross_check(
    client: Any,
    center: tuple[float, float],
    polygon: list[tuple[float, float]],
    blindspots: dict[str, Any] | None,
    refine: RefineResult | None,
    ray_samples: list[tuple[float, float, float | None, float | None]],
    ray_area_km2: float,
    config: GridcheckConfig | None = None,
) -> dict[str, Any] | None:
    """返回 properties.gridcheck；没有可用的圈内网格时返回 None。"""
    cfg = config or GridcheckConfig()
    grid = blindspots or {}
    if grid.get("layout", "polygon") != "polygon" or len(polygon) < 3:
        return None
    cells = [c for c in grid.get("cells") or [] if c.get("in_circle", True)]
    if not cells:
        return None
    spacing = float(grid.get("grid_spacing_m") or cfg.spacing_m)
    cfg = replace(cfg, spacing_m=spacing)
    lattice = _Lattice(cells, spacing)
    chk = _Checker(client, center, polygon, refine, cfg)
    target = cfg.target_s
    exact = float(grid.get("reach_grid_m") or 0) == POINT_GRID_M

    # 1. 圈内格心：新结果已按 10 米细键测过，直接用；旧结果按 50 米量化测得，按细键重测
    base: list[Probe] = []
    for c in cells:
        iy, ix = lattice.index(float(c["lat"]), float(c["lng"]))
        p = chk.add((8 * iy, 8 * ix), 0, "cell", float(c["lat"]), float(c["lng"]))
        if p is None:
            continue
        p.stored_raw_s = c.get("reach_raw_s")
        if exact and c.get("reach_raw_s") is not None and c.get("reach_m") is not None:
            p.measured = True
            p.raw_s = float(c["reach_raw_s"])
            p.dist_m = float(c["reach_m"])
            if refine is not None:
                chk._interp_delay(p)
            else:
                p.blocked = bool(c.get("closure_blocked"))
                if c.get("reach_s") is not None:
                    p.delay_s = float(c["reach_s"]) - p.raw_s
        base.append(p)
    remeasured = 0
    if not exact:
        # 可疑的先测：结论与射线圈冲突的格（超时）在前
        order = sorted(
            base,
            key=lambda p: -(p.stored_raw_s or 0.0),
        )
        await chk.measure(order)
        remeasured = sum(1 for p in base if p.measured)
    corrected = sum(
        1
        for p in base
        if p.stored_raw_s is not None
        and p.raw_s is not None
        and abs(p.raw_s - float(p.stored_raw_s)) > 50.0 / 1.17
    )

    # 2. 外圈：射线圈外、与圈内格八邻接的格网点；可达的再向外扩
    frontier = [p for p in base]
    outer_all: list[Probe] = []
    for _ in range(1 + cfg.outer_rounds):
        ring: list[Probe] = []
        for p in frontier:
            for dy in (-8, 0, 8):
                for dx in (-8, 0, 8):
                    if dy == 0 and dx == 0:
                        continue
                    q = chk.add((p.key[0] + dy, p.key[1] + dx), 0, "outer", lattice=lattice)
                    if q is not None:
                        ring.append(q)
        if not ring:
            break
        ring.sort(key=lambda q: haversine_m(*center, q.lat, q.lng))
        got = await chk.measure(ring)
        outer_all += got
        frontier = [q for q in got if q.status(target) == "reach"]
        if not frontier or chk.budget.exhausted:
            break
    # 预算没测到的外圈点不算数
    for key in [k for k, p in chk.probes.items() if p.kind == "outer" and not p.measured]:
        del chk.probes[key]

    # 绕行线索只用 100 米格网（圈内格 + 外圈）的路网距离，先于子点做：复测每段只花 1 个点对
    clues, candidates = await _clues(chk, lattice, cfg)

    # 3. 50 米子点：结论与射线圈冲突的格——圈内超时、圈外可达
    def conflict(p: Probe) -> bool:
        s = p.status(target)
        if p.inside:
            return s in ("over", "over_unverified", "blocked")
        return s == "reach"

    parents = [p for p in chk.probes.values() if p.level == 0 and p.measured and conflict(p)]
    parents.sort(key=lambda p: (not p.inside, -(p.eff_s or 0.0) if p.inside else (p.eff_s or 0.0)))
    subs: list[Probe] = []
    for p in parents:
        subs += chk.split(p, lattice)
    await chk.measure(subs)
    _drop_unmeasured_children(chk)

    # 4. 25 米点：超时子点紧邻可达区域时再切一层，定位分界
    quarter_parents = []
    for p in list(chk.probes.values()):
        if p.level != 1 or not p.measured or p.status(target) not in ("over", "over_unverified"):
            continue
        if not p.inside:
            continue
        for dy, dx in ((4, 0), (-4, 0), (0, 4), (0, -4)):
            if "reach" in chk.statuses_at((p.key[0] + dy, p.key[1] + dx)):
                quarter_parents.append(p)
                break
    quarters: list[Probe] = []
    for p in quarter_parents:
        quarters += chk.split(p, lattice)
    await chk.measure(quarters, cap=cfg.max_quarter_pairs)
    _drop_unmeasured_children(chk)

    # 5. 只靠插值等待改变结论的点，取它自己的路线核验：先圈内（决定是否画缺口），后圈外
    leaves = [p for p in chk.probes.values() if p.measured and not p.children]
    inside_pending = [p for p in leaves if p.inside and p.status(target) == "over_unverified"]
    outside_pending = [
        p
        for p in leaves
        if not p.inside and p.status(target) == "reach" and p.delay_source == "ray_interp"
    ]
    inside_pending.sort(key=lambda p: (p.eff_s or 0) - target)
    outside_pending.sort(key=lambda p: target - (p.eff_s or 0))
    # 路线预算的先后：决定画不画缺口的圈内点 → 绕行线索的示意路线 → 只影响校核估计的圈外点
    for p in inside_pending:
        await chk.verify_by_route(p)
    # 6. 只凭批量算路判为超时的片（一个点都没取过路线）：取最远的一点，用它自己的路线核验整片。
    #    批量算路自己也会出错（桃浦有一格缓存值 2631 米、精确坐标实测 902 米），不能只信它
    await _check_representatives(chk, leaves, target)
    await _illustrate(chk, clues)
    for p in outside_pending:
        await chk.verify_by_route(p)

    # 7. 缺口：射线圈内、确认超时的叶子点；路线与批量算路结论相反的点（片）记为存疑，不画
    gaps, disputed = _gaps(chk, leaves, clues, target)

    # 8. 受约束局部线性插值 → 校核估计面积
    estimate = _estimate(chk, ray_samples, cfg)

    over_unverified = sum(1 for p in leaves if p.inside and p.status(target) == "over_unverified")
    outside_reach = [p for p in leaves if not p.inside and p.status(target) == "reach"]
    # 圈外的点同理：路线说超时、批量算路加等待说走得到，不算「圈外走得到」
    disputed["outside_points"] = sum(
        1 for p in leaves if not p.inside and p.status(target) == "disputed"
    )
    counts = {"cells": len(base), "outer": 0, "sub50": 0, "sub25": 0}
    for p in chk.probes.values():
        if not p.measured:
            continue
        if p.kind == "outer":
            counts["outer"] += 1
        elif p.level == 1:
            counts["sub50"] += 1
        elif p.level == 2:
            counts["sub25"] += 1
    counts["rays"] = len(ray_samples)
    verified = sum(1 for p in chk.probes.values() if p.delay_source == "own_route")
    gap_area = sum(g["area_m2"] for g in gaps)
    return {
        "version": 1,
        "basis": (
            "另一组网格样本交叉校核：方格的路网距离与不含等待的耗时逐点实测（10 米细键缓存），"
            "过街等待仍取自相邻射线的路线剖面；只有单独取过路线的点用自己的等待。"
            "射线圈仍是主几何，面积、圈内设施、盲区占比与评分都按射线圈计算。"
        ),
        "target_s": target,
        "ray_area_km2": round(ray_area_km2, 4),
        "estimate_area_km2": estimate["area_km2"],
        "estimate_unknown_km2": estimate["unknown_km2"],
        "estimate_rings": estimate["rings"],
        "estimate_holes": estimate["holes"],
        "gaps": gaps,
        "gap_area_km2": round(gap_area / 1e6, 4),
        "clues": [c for c in clues if c["status"] in SHOWN],
        "clue_summary": {
            "edges": candidates["edges"],
            "chains": candidates["chains"],
            "shown": sum(1 for c in clues if c["status"] in SHOWN),
            # 复测 B → A 并不远（contradicted），或路线规划说两格之间不远（disputed）：不显示
            "contradicted": sum(1 for c in clues if c["status"] == "contradicted"),
            "disputed": sum(1 for c in clues if c["status"] == "disputed"),
            "threshold_m": round(cfg.clue_threshold(spacing)),
            "show_m": cfg.clue_show_m,
        },
        "points": counts,
        "remeasured": remeasured,
        "corrected": corrected,
        "over_unverified": over_unverified,
        "outside_reachable": len(outside_reach),
        # 取过路线、路线耗时与「批量算路 + 等待」分处 900 秒两侧的点，以及代表点被路线推翻的整片
        "disputed": disputed,
        "routes": {
            "requested": chk.budget.routes_used,
            "ok": chk.budget.routes_ok,
            "points_verified": verified,
            "representatives": sum(1 for p in chk.probes.values() if p.representative),
        },
        "usage": {"matrix_pairs": chk.budget.pairs_used, "routes": chk.budget.routes_used},
        "budget": {"max_pairs": cfg.max_pairs, "max_routes": cfg.max_routes},
        "stopped": chk.budget.exhausted,
        "params": {
            "kappa": cfg.kappa,
            "eps_m": cfg.eps_m,
            "clue_show_m": cfg.clue_show_m,
            "raster_m": cfg.raster_m,
            "max_edge_m": cfg.max_edge_m,
        },
        "notes": [
            "缺口只画确认超过 900 秒的点：不计过街等待已超时，"
            "或用这个点自己的步行路线补上等待后超时。"
            "取过路线的点，路线自己的耗时也要超过 900 秒；两者分处 900 秒两侧的记为存疑、不画。"
            "一个点都没取过路线的片，取最远的一点核验，路线说走得到的整片记为存疑。",
            "绕行线索按三角不等式推得，400 米为经验筛选阈值；60 米吸附余量与 ±50 米位置误差"
            "都是经验值，不是已证明的误差上限。线索画在两格公共边上，不是障碍的真实位置。",
            "校核估计面积是另一种插值估计（三角网受约束局部线性插值，10 米栅格积分），"
            "不替代射线圈。",
        ],
    }


def _drop_unmeasured_children(chk: _Checker) -> None:
    """预算没测到的子点删掉，父格保持叶子，结论仍取父格的。"""
    for key in [k for k, p in chk.probes.items() if p.level > 0 and not p.measured]:
        del chk.probes[key]
    for p in chk.probes.values():
        p.children = [k for k in p.children if k in chk.probes]


async def _clues(chk: _Checker, lattice: _Lattice, cfg: GridcheckConfig):
    """100 米格网上的绕行线索：相邻格路网距离之差超过 κ·s + 2ε 的边，串成段，强的才复测显示。"""
    level0 = {k: p for k, p in chk.probes.items() if p.level == 0 and p.dist_m is not None}
    s = cfg.spacing_m
    limit = cfg.clue_threshold(s)
    edges = []
    for (y, x), p in level0.items():
        for dy, dx in ((8, 0), (0, 8)):
            q = level0.get((y + dy, x + dx))
            if q is None:
                continue
            jump = abs(p.dist_m - q.dist_m)  # type: ignore[operator]
            if jump <= limit:
                continue
            near, far = (p, q) if p.dist_m < q.dist_m else (q, p)  # type: ignore[operator]
            # 两格公共边的两个端点（1/8 格距单位）
            side = ((y + 4, x - 4), (y + 4, x + 4)) if dy else ((y - 4, x + 4), (y + 4, x + 4))
            edges.append({"near": near, "far": far, "jump": jump, "side": side})

    # 共用端点的边连成一段
    parent = list(range(len(edges)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    by_end: dict[tuple[int, int], list[int]] = {}
    for i, e in enumerate(edges):
        for end in e["side"]:
            by_end.setdefault(end, []).append(i)
    for members in by_end.values():
        for i in members[1:]:
            parent[find(i)] = find(members[0])
    groups: dict[int, list[int]] = {}
    for i in range(len(edges)):
        groups.setdefault(find(i), []).append(i)
    chains = sorted(groups.values(), key=lambda g: -max(edges[i]["jump"] for i in g))

    clues = []
    n = 0
    for members in chains:
        strongest = max(members, key=lambda i: edges[i]["jump"])
        e = edges[strongest]
        if e["jump"] < cfg.clue_show_m:
            continue
        # 只显示与射线圈有关的段：最强那条边至少有一端是圈内格。圈外远处的线索只计数
        if not (e["near"].inside or e["far"].inside) or n >= cfg.max_clues:
            continue
        n += 1
        path = _chain_path([edges[i]["side"] for i in members], lattice)
        near, far = e["near"], e["far"]
        verification = {
            "from": [round(near.lng, 6), round(near.lat, 6)],
            "to": [round(far.lng, 6), round(far.lat, 6)],
            "straight_m": round(haversine_m(near.lat, near.lng, far.lat, far.lng)),
            "center_to_from_m": round(near.dist_m),
            "center_to_to_m": round(far.dist_m),
            "bound_m": round(e["jump"]),
            "measured_m": None,
            "route_m": None,
            "route_path": None,
        }
        status = "unverified"
        measured = await _measure_pair(chk, near, far)
        if measured is not None:
            verification["measured_m"] = round(measured)
            status = "confirmed" if measured > limit else "contradicted"
        clues.append(
            {
                "id": n,
                "path": path,
                "edges": len(members),
                "max_jump_m": round(e["jump"]),
                "status": status,
                "verification": verification,
            }
        )
    return clues, {"edges": len(edges), "chains": len(chains)}


async def _illustrate(chk: _Checker, clues: list[dict]) -> None:
    """给已实测的线索取一条 B → A 步行路线，只作地图示意：路线规划与批量算路的距离口径不同。"""
    for clue in clues:
        if clue["status"] != "confirmed" or chk.budget.routes_used >= chk.budget.routes:
            continue
        v = clue["verification"]
        chk.budget.routes_used += 1
        route = await chk.client.walking_route(
            (v["from"][1], v["from"][0]), (v["to"][1], v["to"][0]), fine_m=POINT_GRID_M
        )
        if route is None:
            continue
        pts = [
            (float(a), float(b))
            for step in route.get("steps") or []
            for a, b in step.get("path") or []
        ]
        chk.budget.routes_ok += 1
        v["route_m"] = round(float(route.get("distance_m") or 0))
        if v["route_m"] > chk.cfg.clue_threshold(v["straight_m"]):
            v["route_path"] = [
                [round(lng, 6), round(lat, 6)] for lat, lng in simplify_path(pts, 8.0)
            ]
        else:
            # 路线规划说两格之间并不远：两个接口吸附到了不同的路上，线索存疑，也不画示意路线
            clue["status"] = "disputed"


async def _measure_pair(chk: _Checker, near: Probe, far: Probe) -> float | None:
    """直接测 near → far：三角不等式约束的正是这个方向，不假设双向对称。两端都用 10 米细键。"""
    budget = chk.budget
    if budget.exhausted in ("quota", "error"):
        return None
    missing = getattr(chk.client, "missing_pairs", None)
    cost = (
        missing(
            "walk",
            [(near.lat, near.lng)],
            [(far.lat, far.lng)],
            dest_grid_m=POINT_GRID_M,
            origin_grid_m=POINT_GRID_M,
        )
        if missing
        else 1
    )
    if cost and budget.pairs_used + cost > budget.pairs:
        budget.exhausted = budget.exhausted or "budget"
        return None
    before = chk._pairs_now()
    try:
        rows = await chk.client.walking_matrix_grid(
            [(near.lat, near.lng)],
            [(far.lat, far.lng)],
            dest_grid_m=POINT_GRID_M,
            origin_grid_m=POINT_GRID_M,
        )
    except QuotaExhaustedError:
        budget.exhausted = "quota"
        return None
    except BaiduApiError:
        return None
    after = chk._pairs_now()
    budget.pairs_used += (after - before) if before is not None else cost
    entry = rows[0][0] if rows and rows[0] else None
    return float(entry["distance_m"]) if entry else None


def _chain_path(sides, lattice: _Lattice) -> list[list[list[float]]]:
    """把一段线索的公共边连成折线：经过各条边的中点，单独一条边就画它自己。"""
    mids = [((a[0] + b[0]) / 2, (a[1] + b[1]) / 2) for a, b in sides]
    if len(sides) == 1:
        (a, b) = sides[0]
        pts = [a, b]
        return [[_lnglat(lattice, *pts[0]), _lnglat(lattice, *pts[1])]]
    ends: dict[tuple[int, int], list[int]] = {}
    for i, side in enumerate(sides):
        for end in side:
            ends.setdefault(end, []).append(i)
    nbr: dict[int, set[int]] = {i: set() for i in range(len(sides))}
    for members in ends.values():
        for i in members:
            nbr[i] |= set(members) - {i}
    seen: set[int] = set()
    paths = []
    order = sorted(nbr, key=lambda i: (len(nbr[i]), sides[i]))
    for start in order:
        if start in seen:
            continue
        line = [start]
        seen.add(start)
        cur = start
        while True:
            nxt = [j for j in sorted(nbr[cur]) if j not in seen]
            if not nxt:
                break
            cur = nxt[0]
            seen.add(cur)
            line.append(cur)
        if len(line) == 1:
            pts = list(sides[line[0]])
        else:
            # 首尾补上没和邻边相接的那个端点，线才盖住整条公共边
            first = _free_end(sides[line[0]], sides[line[1]])
            last = _free_end(sides[line[-1]], sides[line[-2]])
            pts = [first, *(mids[i] for i in line), last]
        latlng = [lattice.at(y, x) for y, x in pts]
        simple = simplify_path(latlng, 20.0)
        paths.append([[round(lng, 6), round(lat, 6)] for lat, lng in simple])
    return paths


def _free_end(side, other) -> tuple[int, int]:
    return side[0] if side[0] not in other else side[1]


def _lnglat(lattice: _Lattice, y8: float, x8: float) -> list[float]:
    lat, lng = lattice.at(y8, x8)  # type: ignore[arg-type]
    return [round(lng, 6), round(lat, 6)]


def _components(probes: list[Probe]) -> list[list[Probe]]:
    """八邻接（含跨层级相邻）并成片。key 以 1/8 格距为单位，半边长 = 4 / 2^level。"""
    idx = list(range(len(probes)))

    def find(i: int) -> int:
        while idx[i] != i:
            idx[i] = idx[idx[i]]
            i = idx[i]
        return i

    for i, a in enumerate(probes):
        ha = 4 / (2**a.level)
        for j in range(i + 1, len(probes)):
            b = probes[j]
            hb = 4 / (2**b.level)
            if (
                abs(a.key[0] - b.key[0]) <= ha + hb + 1e-9
                and abs(a.key[1] - b.key[1]) <= ha + hb + 1e-9
            ):
                idx[find(j)] = find(i)
    groups: dict[int, list[Probe]] = {}
    for i, p in enumerate(probes):
        groups.setdefault(find(i), []).append(p)
    return list(groups.values())


def _gap_candidates(leaves: list[Probe], target: float) -> list[Probe]:
    """射线圈内可能画成缺口的叶子点：确认超时、受围挡阻断，或取过路线后存疑。"""
    return [p for p in leaves if p.inside and p.status(target) in ("over", "blocked", "disputed")]


def _route_confirmed(p: Probe, target: float) -> bool:
    return p.route_s is not None and p.status(target) in ("over", "blocked")


async def _check_representatives(chk: _Checker, leaves: list[Probe], target: float) -> None:
    """一个点都没被路线确认过的片：取批量算路最远的一点，单独取它自己的步行路线核验。

    选最远的一点，是因为批量算路的错误值（吸附到别的路上、缓存混用）往往就是最极端的那个，
    它也是缺口卡片上显示的「最远一点」。路线预算用完时不核验，片照旧按批量算路的结论画。
    """
    for members in _components(_gap_candidates(leaves, target)):
        if any(_route_confirmed(p, target) for p in members):
            continue
        pool = [p for p in members if p.status(target) == "over" and p.route_s is None]
        if not pool:
            continue
        worst = max(pool, key=lambda p: (p.eff_s or 0.0, p.key))
        await chk.verify_by_route(worst)
        if worst.route_s is not None:
            worst.representative = True


def _gaps(
    chk: _Checker, leaves: list[Probe], clues: list[dict], target: float
) -> tuple[list[dict], dict[str, Any]]:
    """返回 (缺口, 存疑统计)。

    先把候选点（超时、阻断、存疑）并成片：代表点被自己的路线推翻的片整片存疑，不画；
    其余片只去掉存疑的点，剩下的确认点重新并成片画出。
    """
    candidates = _gap_candidates(leaves, target)
    kept: list[Probe] = []
    dropped: list[Probe] = []
    disputed_groups = 0
    for members in _components(candidates):
        rep = next((p for p in members if p.representative), None)
        if rep is not None and rep.status(target) == "disputed":
            # 这片只有批量算路说走不到，最远一点的路线却说 15 分钟内走得到：整片存疑
            disputed_groups += 1
            dropped += members
            continue
        for p in members:
            (dropped if p.status(target) == "disputed" else kept).append(p)
    disputed = {
        "points": len(dropped),
        "groups": disputed_groups,
        "area_m2": round(sum(_inside_area(chk, p) for p in dropped)),
    }
    if not kept:
        return [], disputed
    by_root: dict[tuple[int, int], list[Probe]] = {}
    for p in leaves:
        by_root.setdefault(root_key(p.key), []).append(p)
    blocks = sorted(_components(kept), key=lambda g: -sum(_inside_area(chk, p) for p in g))

    out = []
    for members in blocks:
        area = sum(_inside_area(chk, p) for p in members)
        if area <= 0:
            continue
        worst = max(members, key=lambda p: p.eff_s or 0.0)
        lat_c = sum(p.lat for p in members) / len(members)
        lng_c = sum(p.lng for p in members) / len(members)
        anchor = min(members, key=lambda p: (p.lat - lat_c) ** 2 + (p.lng - lng_c) ** 2)
        near_clues = []
        for c in clues:
            if c["status"] not in SHOWN:
                continue
            d = min(
                haversine_m(p.lat, p.lng, pt[1], pt[0])
                for p in members
                for line in c["path"]
                for pt in line
            )
            if d <= 150:
                near_clues.append(c["id"])
        out.append(
            {
                "id": "",  # 下面按面积排好后统一编号
                "area_m2": round(area),
                "anchor": {"lat": round(anchor.lat, 6), "lng": round(anchor.lng, 6)},
                "leaves": [_leaf(chk, p) for p in sorted(members, key=lambda p: p.key)],
                "worst": _leaf(chk, worst),
                # 取过自己的步行路线、路线耗时也超过 900 秒的点数；0 表示整片只有批量算路的结论
                "route_confirmed": sum(1 for p in members if _route_confirmed(p, target)),
                "clues": near_clues,
                # 有几个盲区格只是部分走不到：格内子点有的可达、有的超时，按子点分别保留
                "partial_cells": sum(
                    1
                    for r in {root_key(m.key) for m in members if m.level > 0}
                    if any(q.status(target) == "reach" for q in by_root.get(r, []))
                ),
            }
        )
    for n, g in enumerate(out, start=1):
        g["id"] = f"G{n}"
    return out, disputed


def _inside_area(chk: _Checker, p: Probe) -> float:
    """叶子方格落在射线圈内的面积（4×4 采样估计）。"""
    half = p.size_m / 2
    dlat = half / METERS_PER_DEG_LAT
    dlng = half / meters_per_deg_lng(p.lat)
    hits = 0
    for i in range(4):
        for j in range(4):
            lat = p.lat - dlat + (2 * i + 1) * dlat / 4
            lng = p.lng - dlng + (2 * j + 1) * dlng / 4
            hits += point_in_polygon(lat, lng, chk.polygon)
    return p.size_m * p.size_m * hits / 16


def _leaf(chk: _Checker, p: Probe) -> dict[str, Any]:
    return {
        "lat": round(p.lat, 6),
        "lng": round(p.lng, 6),
        "size_m": p.size_m,
        "raw_s": round(p.raw_s) if p.raw_s is not None else None,
        "eff_s": (round(p.eff_s) if p.eff_s is not None and not math.isinf(p.eff_s) else None),
        "blocked": p.blocked,
        "dist_m": round(p.dist_m) if p.dist_m is not None else None,
        "straight_m": round(haversine_m(*chk.center, p.lat, p.lng)),
        "delay_source": p.delay_source,
        # 取过自己的步行路线时：路线给的耗时与距离（与批量算路分属两个接口，可能吸附到不同的路上）
        "route_s": round(p.route_s) if p.route_s is not None else None,
        "route_m": round(p.route_m) if p.route_m is not None else None,
        "off_network": p.off_network,
    }


def _estimate(chk: _Checker, ray_samples, cfg: GridcheckConfig) -> dict[str, Any]:
    vertices: list[Vertex] = [Vertex(0.0, 0.0, 0.0, 0.0)]
    seen: set[tuple[int, int]] = {(0, 0)}

    def push(lat: float, lng: float, t: float | None, d: float | None) -> None:
        x, y = chk.xy(lat, lng)
        key = (round(x), round(y))  # 1 米内的重复点只留一个
        if key in seen:
            return
        seen.add(key)
        vertices.append(Vertex(x, y, t, d))

    for lat, lng, t, d in ray_samples:
        push(lat, lng, t, d)
    for p in sorted(chk.probes.values(), key=lambda p: p.key):
        if not p.measured:
            continue
        t = None if p.raw_s is None else (math.inf if p.blocked else p.raw_s + p.delay_s)
        push(p.lat, p.lng, t, p.dist_m)
    # 三角形边长上限按 100 米格网调的（3 倍格距）；格网更粗时相邻格心本来就更远，跟着放宽
    max_edge = max(cfg.max_edge_m, 3 * cfg.spacing_m)
    raster = interpolate(
        vertices, res_m=cfg.raster_m, kappa=cfg.kappa, eps_m=cfg.eps_m, max_edge_m=max_edge
    )
    area, _ = areas_m2(raster, cfg.target_s)
    # 未知只报射线圈里的部分：圈外远处（射线最外两档之间）三角形边长超限，本来就不关心
    unknown = 0.0
    for i, row in enumerate(raster.values):
        for j, v in enumerate(row):
            if v is None or not math.isnan(v):
                continue
            x, y = raster.center(i, j)
            lat = chk.center[0] + y / METERS_PER_DEG_LAT
            lng = chk.center[1] + x / chk.mpl
            if point_in_polygon(lat, lng, chk.polygon):
                unknown += raster.res * raster.res
    rings = [r for r in contour_rings(raster, cfg.target_s) if abs(ring_area(r)) >= 400.0]
    holes = classify_rings(rings)
    out_rings = []
    for ring in rings:
        latlng = [
            (chk.center[0] + y / METERS_PER_DEG_LAT, chk.center[1] + x / chk.mpl) for x, y in ring
        ]
        simple = simplify_path([*latlng, latlng[0]], 3.0)
        out_rings.append([[round(lng, 6), round(lat, 6)] for lat, lng in simple])
    return {
        "area_km2": round(area / 1e6, 4),
        "unknown_km2": round(unknown / 1e6, 4),
        "rings": out_rings,
        "holes": holes,
        "barrier_triangles": raster.barrier_triangles,
    }
