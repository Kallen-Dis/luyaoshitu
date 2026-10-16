"""网格级服务盲区识别（真实路网口径）。

评分细则要求"识别出周边 1 公里内没有菜市场、药店或小学的服务盲区点位"。
品类计数只能回答"整个圈内有没有"，回答不了"哪一片居民走不到"——
一个圈内有两家药店的社区，被铁路切开的另一侧照样是盲区。故按网格逐点判定：

1. 在 15 分钟步行圈内按 100 米铺网格（曹杨约 144 格、桃浦约 116 格）。体检的是这个生活圈，
   灰色区域只标圈内；但设施照样检索到圈外 1 公里，圈边上的居民走到圈外的药店也算有；
2. 中心点到各网格做一次批量算路，得到步行耗时，即热力图的强度；
   步行时再用等时圈各方向的路线剖面补回过街等待（见 isochrone/refine.py）。
   格心终点按 10 米细键缓存：按 50 米量化时，同一缓存格里别的点测出的结果会被当成这一格的，
   2026-10-09 在桃浦复测出 6 格偏差超过 50 米，最大一格缓存值 2631 米、精确坐标实测 902 米；
3. 对每个关键品类，为各网格预筛若干直线最近的候选设施，
   用**多起点批量算路**求真实路网步行距离；
4. 步行 1 公里内一家都到不了（或该品类附近无设施）的网格，标为该品类的盲区点位。

**直线距离只做经验下界，从不用来判定「够得着」。** 为端点吸附与缓存近似留出
30 米余量，直线超过 1030 米才剪枝；余量内的设施必须实测步行距离才能下结论。
30 米不是已证实的全局误差上界，阈值边缘仍需现场复核。

配额是这里的主要设计约束：批量算路的日额度按点对计量，
朴素做法（每个网格对每个设施测距）的点对数是网格数 × 设施数 × 品类数，
很快就会耗尽。三条措施把它压下来，并保留余量假设与未知状态：

1. **直线下界剪枝**：直线就超标的设施不进候选，连请求都不必发；
2. **按最近候选分组，逐轮测距，够近就停**：判定"1 公里内有没有"只需找到一个
   达标设施。每轮给未决网格各分配"第 k 近"的候选，把共用同一候选的网格并成一次
   请求（矩阵退化成一列，点对数等于网格数）；
3. **候选耗尽即可判定**：直线阈值内的候选全测完仍无一达标，就是严格的盲区。

**学校按校门测。** 有面积的设施（目前是小学）终点取它的入口（导航点 + 校门，见 poi/entries.py），
到这家的距离取最近的入口；剪枝的直线下界也按入口算。拿不到入口的退回坐标点。

**施工围挡。** 位于围挡内的设施视为暂不可用；判为可达的网格-设施点对，若按椭圆条件
路线可能经过围挡，再取一条步行路线核验，穿过围挡的按走不到处理、换下一家候选。

另一条不容妥协的取舍：**测距失败判为"未知"而非"盲区"**。批量算路对不可达点与
请求失败都返回空值，两者无法区分。既然本项目的核心输出就是"这里缺设施"，
就不能让一次接口抖动伪装成盲区。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import Any

from ..baidu.client import BaiduMapClient
from ..baidu.errors import QuotaExhaustedError
from ..isochrone.geometry import (
    METERS_PER_DEG_LAT,
    disc_cells,
    first_entry_along,
    grid_points,
    has_polygon,
    haversine_m,
    meters_per_deg_lng,
    point_in_polygon,
)
from ..isochrone.refine import Closure, RefineResult
from ..poi.catalog import KEY_CATEGORIES, Category
from ..poi.collect import CoverageResult, Poi
from ..poi.entries import ENTRY_GRID_M, destinations, straight_m
from ..poi.fresh import FRESH, VERSION, eligible, fields

# 中心 → 格心测时的终点缓存粒度（米）。格心相距 100 米，但别的分析（换中心、50 米子点、
# 出行起点）会在同一个 50 米缓存格里测别的点，按 50 米量化就可能拿到别人的结果
REACH_GRID_M = 10.0


@dataclass(frozen=True)
class BlindspotConfig:
    # 网格间距。100 米在 15 分钟步行圈内约 120~150 格（曹杨 1.41 km² 约 144 格），
    # 与地图上画的方格一一对应；灰色区域的轮廓最多比圈边界外凸半格（50 米）。
    grid_spacing_m: float = 100.0

    # 只对 disc 布点有意义：以中心为圆心的网格半径。现版只在圈内布点，这个值另外
    # 决定设施检索与标注查询至少罩住多大范围（再加 1 公里判定阈值，即 2.5 公里）。
    extent_m: float = 1500.0

    # polygon：只在 15 分钟步行圈内布点（现版口径：体检的是这个生活圈，灰色区域只标圈内）；
    # disc：以中心为圆心铺满 extent_m（2026-10 之前的口径，八成格子在圈外，已不再默认使用）
    layout: str = "polygon"

    # 命题给定的盲区判定阈值：步行 1 公里内无此类设施。
    walk_limit_m: float = 1000.0

    # 每个网格最多测几轮候选。一轮 = 给未决网格各分配"第 k 近"的候选并测距。
    #
    # 达到上限仍未决的网格判为"未知"而不是盲区：此时只能说"最近的几家走不到"，
    # 不能断言"一家都走不到"。设 4 是因为直线阈值内候选通常不足 4 家，
    # 实际多在候选耗尽时就已严格判定，很少触到这个上限。
    max_rounds: int = 4

    # 围挡核验最多取多少条步行路线。超出的点对无法核验，判为"未知"。
    max_closure_checks: int = 80

    # 某品类判定成功的网格不到这个比例时，不给出盲区占比（也就不进评分）。
    # 配额在判定途中用完时，只测到的一小撮网格算出的占比没有代表性。
    min_judged_share: float = 0.5

    # 50 米叠加细分：100 米判定之后，对每个品类的「边界格」（与四邻结论不同）与「临界格」
    # （最近一家步行距离在阈值 ± refine_critical_m 内），把格切成 4 个 50 米子格分别实测。
    # 子格作起点按 10 米细键缓存；没细分的品类、没细分的格都继承父格结论。
    # 子格中心落在射线圈外的不测、不计（与 100 米格「格心在圈内才算」同一条规则）。
    refine_boundary: bool = True
    refine_critical_m: float = 50.0
    # 细分最多新增的批量算路点对（命中缓存不计），是硬上限：每批请求发出前按缓存缺口算成本，
    # 超出剩余额度的那批不发，涉及的子格这一品类照旧继承父格
    refine_max_pairs: int = 1500
    # 融合扩大/挖孔后沿用射线网格原点与步长，避免格心平移导致重复测量。
    grid_alignment: Any = None


@dataclass
class CellResult:
    lat: float
    lng: float
    # 中心点步行到该网格的耗时（已补回过街等待），热力图强度由它决定。
    # None 表示测距失败或受围挡阻断。
    reach_s: float | None = None
    # 未补过街等待的批量算路耗时
    reach_raw_s: float | None = None
    # 中心到该格的路网距离（米）。网格交叉校核用它找绕行线索：距离每格独立测得，
    # 不像耗时那样含有从射线插值来的过街等待
    reach_m: float | None = None
    # 品类 -> 最近设施的真实路网步行距离（米）。None 表示无法判定。
    nearest_m: dict[str, float | None] = field(default_factory=dict)
    # 1 公里内确无此类设施的品类
    missing: list[str] = field(default_factory=list)
    # 测距失败、既不能判有也不能判无的品类
    unknown: list[str] = field(default_factory=list)
    unknown_reasons: dict[str, str] = field(default_factory=dict)
    # 是否落在 15 分钟等时圈内
    in_circle: bool = True
    # 从中心过来的路线被施工围挡挡住
    closure_blocked: bool = False
    # 50 米叠加细分：只有细分过的格才有，按西北、东北、西南、东南排
    quarters: list[CellResult] | None = None
    # 子格自己：位置标签与实测过的品类（其余品类继承父格）
    q: str | None = None
    measured: list[str] = field(default_factory=list)

    @property
    def is_blind(self) -> bool:
        return bool(self.missing)

    def quarter_dict(self) -> dict[str, Any]:
        return {
            "q": self.q,
            "lat": round(self.lat, 6),
            "lng": round(self.lng, 6),
            # 子格中心是否在射线圈内；圈外的子格不测、不判定，也不进任何计数
            "in_circle": self.in_circle,
            "nearest_m": {
                k: (round(v) if v is not None else None) for k, v in self.nearest_m.items()
            },
            "missing": self.missing,
            "unknown": self.unknown,
            "unknown_reasons": self.unknown_reasons,
            "measured": self.measured,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "lat": round(self.lat, 6),
            "lng": round(self.lng, 6),
            "reach_s": round(self.reach_s) if self.reach_s is not None else None,
            "reach_raw_s": round(self.reach_raw_s) if self.reach_raw_s is not None else None,
            "reach_m": round(self.reach_m) if self.reach_m is not None else None,
            "nearest_m": {
                k: (round(v) if v is not None else None) for k, v in self.nearest_m.items()
            },
            "missing": self.missing,
            "unknown": self.unknown,
            "unknown_reasons": self.unknown_reasons,
            "in_circle": self.in_circle,
            "closure_blocked": self.closure_blocked,
            **({"quarters": [q.quarter_dict() for q in self.quarters]} if self.quarters else {}),
        }


@dataclass
class Refinement:
    """50 米叠加细分的统计。"""

    spacing_m: float
    max_pairs: int = 0
    parents: dict[str, int] = field(default_factory=dict)  # 品类 → 细分的格数
    pairs: int = 0  # 新增点对（命中缓存不计），受 max_pairs 约束
    requested_pairs: int = 0  # 发给客户端的点对总数，含命中缓存的；与缓存状态无关
    stopped: list[str] = field(default_factory=list)  # 预算用完、没细分完的品类
    fallback: dict[str, int] = field(default_factory=dict)  # 品类 → 因预算退回继承的子格数

    def as_dict(self, cells: list[CellResult]) -> dict[str, Any]:
        split = [c for c in cells if c.quarters]
        quarters = [q for c in split for q in c.quarters or []]
        return {
            "spacing_m": self.spacing_m,
            "split_cells": len(split),
            "by_category": self.parents,
            "measured_quarters": sum(1 for q in quarters if q.measured),
            # 中心落在射线圈外的子格：不测、不计，父格按圈内子格的面积计
            "outside_quarters": sum(1 for q in quarters if not q.in_circle),
            "pairs": self.pairs,
            "requested_pairs": self.requested_pairs,
            "max_pairs": self.max_pairs,
            "stopped_categories": self.stopped,
            "fallback_quarters": self.fallback,
            "basis": (
                "100 米判定后，对每个品类与四邻结论不同的边界格、"
                "以及最近一家步行距离贴近阈值的临界格，切成 4 个 50 米子格分别实测"
                "（子格作起点按 10 米细键缓存）；其余格与其余品类继承父格。"
                "子格中心落在当前主范围外的不测、不计。"
                "占比与灰色区域按面积计，子格面积替代父格，不重复统计。"
                "新增点对有硬上限：每批请求前按缓存缺口计成本，超出剩余额度的那批不发，"
                "涉及的子格这一品类继承父格。"
            ),
        }


@dataclass
class ClosureCheck:
    """围挡核验的预算与统计，跨品类共享。"""

    budget: int
    checked: int = 0
    blocked: int = 0
    unverified: int = 0
    excluded_places: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "checked_pairs": self.checked,
            "blocked_pairs": self.blocked,
            "unverified_pairs": self.unverified,
            "excluded_places": self.excluded_places,
        }


@dataclass
class BlindspotResult:
    center: tuple[float, float]
    cells: list[CellResult]
    config: BlindspotConfig
    # 品类 -> 被判为盲区的网格占比（0~1）。只统计判定成功的网格。
    blind_ratio: dict[str, float] = field(default_factory=dict)
    # 直线剪枝直接完成判定的次数（网格 × 品类）：直线就超标的候选无需任何请求。
    pruned_decisions: int = 0
    # 朴素做法的点对数（网格数 × 设施数 × 品类数）。与实发点对数对比，
    # 是「配额优化省下多少」最直观的口径。
    naive_matrix_pairs: int = 0
    delay_applied: bool = False
    closures: tuple[Closure, ...] = ()
    closure_check: ClosureCheck | None = None
    # 判定成功的网格不足、没有给出占比的品类
    incomplete_categories: list[str] = field(default_factory=list)
    refinement: Refinement | None = None

    @property
    def blind_cells(self) -> list[CellResult]:
        return [c for c in self.cells if c.is_blind]

    @property
    def blind_units(self) -> float:
        """缺任一类的面积，按 100 米格计（细分过的格按圈内子格各 1/4 计）。"""
        return sum(w for u, w in area_units(self.cells) if u.missing)

    def as_dict(self) -> dict[str, Any]:
        cells = self.cells
        max_reach = max((c.reach_s for c in cells if c.reach_s is not None), default=None)
        return {
            "fresh_rule_version": VERSION,
            "basis": "network",
            "layout": self.config.layout,
            "grid_spacing_m": self.config.grid_spacing_m,
            # 中心 → 格心的终点缓存粒度；没有这个字段的旧结果按 50 米量化测得
            "reach_grid_m": REACH_GRID_M,
            "extent_m": self.config.extent_m if self.config.layout == "disc" else None,
            "walk_limit_m": self.config.walk_limit_m,
            "cell_count": len(cells),
            # 按面积计的 100 米格数：细分过的格按子格各 1/4 计，可能带小数
            "blind_count": round(self.blind_units, 2),
            "in_circle_count": sum(1 for c in cells if c.in_circle),
            "max_reach_s": round(max_reach) if max_reach is not None else None,
            "blind_ratio": {k: round(v, 3) for k, v in self.blind_ratio.items()},
            "cells": [c.as_dict() for c in cells],
            "pruned_decisions": self.pruned_decisions,
            "naive_matrix_pairs": self.naive_matrix_pairs,
            "delay_applied": self.delay_applied,
            "closures": [c.as_dict() for c in self.closures],
            "closure_check": self.closure_check.as_dict() if self.closure_check else None,
            "incomplete_categories": self.incomplete_categories,
            "refinement": self.refinement.as_dict(cells) if self.refinement else None,
        }


def area_units(cells: list[CellResult]) -> list[tuple[CellResult, float]]:
    """按面积计数的单元：没细分的格占 1；细分过的格由圈内子格各占 1/4 替代，不和父格重复统计。
    子格中心落在射线圈外的不计，与 100 米格「格心在圈内才算」是同一条规则。"""
    out: list[tuple[CellResult, float]] = []
    for c in cells:
        if c.quarters:
            out += [(q, 0.25) for q in c.quarters if q.in_circle]
        else:
            out.append((c, 1.0))
    return out


def rank_candidates(
    cell: CellResult, pois: list[Poi], limit_m: float, entrances: bool = True
) -> list[int]:
    """按直线距离给某网格排出候选设施，只保留直线距离在阈值内的。

    直线距离是步行距离的下界，故直线就超标的设施不可能在阈值内走到，
    连请求都不必发。返回的是 pois 的下标，按由近及远排列。

    有入口的设施（学校）按最近的入口算直线：按坐标点剪枝会把「门很近、点很远」的学校误剪掉。
    entrances=False 只给直线法对照用（传统做法看的就是设施坐标）。
    """

    def dist(p: Poi) -> float:
        if entrances:
            return straight_m(cell.lat, cell.lng, p)
        return haversine_m(cell.lat, cell.lng, p.lat, p.lng)

    scored = [(dist(p), i) for i, p in enumerate(pois)]
    return [i for d, i in sorted(scored) if d <= limit_m]


def route_may_touch(
    a: tuple[float, float],
    b: tuple[float, float],
    route_m: float,
    closure: Closure,
) -> bool:
    """长度为 route_m 的路线能否经过围挡。

    路线上任一点 P 都满足 |A−P| + |P−B| ≤ route_m（直线不长于路线），即 P 落在以 A、B
    为焦点的椭圆内。围挡圆心 C、半径 ρ 时，若 |A−C| + |C−B| − 2ρ > route_m，
    圆与椭圆不相交，路线必然不经过围挡，无需核验。
    """
    via = haversine_m(a[0], a[1], closure.lat, closure.lng) + haversine_m(
        closure.lat, closure.lng, b[0], b[1]
    )
    return via - 2 * closure.radius_m <= route_m


def available_pois(pois: list[Poi], closures: tuple[Closure, ...]) -> tuple[list[Poi], int]:
    """剔除落在围挡内的设施（施工期间暂不可用）。返回 (可用设施, 剔除数)。"""
    if not closures:
        return pois, 0
    kept = [
        p
        for p in pois
        if not any(haversine_m(p.lat, p.lng, c.lat, c.lng) <= c.radius_m for c in closures)
    ]
    return kept, len(pois) - len(kept)


async def _route_blocked(
    client: BaiduMapClient,
    a: tuple[float, float],
    b: tuple[float, float],
    closures: tuple[Closure, ...],
    fine_m: float | None = None,
) -> bool | None:
    """取一条步行路线核验是否穿过围挡。None 表示路线取不到、无法核验。"""
    route = (
        await client.walking_route(a, b, fine_m=fine_m)
        if fine_m
        else await client.walking_route(a, b)
    )
    if route is None:
        return None
    points = [
        (float(lat), float(lng))
        for step in route.get("steps") or []
        for lat, lng in step.get("path") or []
    ]
    if len(points) < 2:
        return None
    return first_entry_along(points, [c.as_circle() for c in closures]) is not None


async def _judge_category(
    client, cells, category, coverage, cfg, closures=(), check=None, origin_grid_m=None
):
    if category.name != FRESH or FRESH in coverage.failed:
        return await _judge_candidates(
            client, cells, category, coverage, cfg, closures, check, origin_grid_m
        )
    pois = coverage.pois_of(FRESH)
    accepted = [p for p in pois if eligible(p)]
    uncertain = [p for p in pois if fields(p)["fresh_status"] == "pending"]
    available, _ = available_pois(accepted, closures)
    free = {id(c) for c in cells if not rank_candidates(c, available, cfg.walk_limit_m + 30)}
    available_pending, _ = available_pois(uncertain, closures)
    naive_pending = len(cells) * sum(len(destinations(p)) for p in available_pending)

    def subset(selected):
        return replace(
            coverage,
            results=[
                replace(r, pois=selected) if r.category == FRESH else r for r in coverage.results
            ],
        )

    pruned, naive = await _judge_candidates(
        client, cells, category, subset(accepted), cfg, closures, check, origin_grid_m
    )
    # 已有可信候选可达时，不为核对普通超市额外消耗点对。
    unresolved = [c for c in cells if FRESH in c.missing or FRESH in c.unknown]
    if not uncertain or not unresolved:
        return pruned, naive + naive_pending
    probes = [CellResult(c.lat, c.lng) for c in unresolved]
    await _judge_candidates(
        client, probes, category, subset(uncertain), cfg, closures, check, origin_grid_m
    )
    pruned -= sum(
        id(c) in free and bool(rank_candidates(c, available_pending, cfg.walk_limit_m + 30))
        for c in unresolved
    )
    for cell, probe in zip(unresolved, probes, strict=True):
        reachable = FRESH not in probe.missing and FRESH not in probe.unknown
        if reachable or FRESH in probe.unknown:
            if FRESH in cell.missing:
                cell.missing.remove(FRESH)
            if FRESH not in cell.unknown:
                cell.unknown.append(FRESH)
            cell.unknown_reasons[FRESH] = (
                "fresh_pending" if reachable else "fresh_pending_distance_unknown"
            )
            cell.nearest_m[FRESH] = None  # 不冒充已知买菜门店的距离。
    return pruned, naive + naive_pending


async def _judge_candidates(
    client: BaiduMapClient,
    cells: list[CellResult],
    category: Category,
    coverage: CoverageResult,
    cfg: BlindspotConfig,
    closures: tuple[Closure, ...] = (),
    check: ClosureCheck | None = None,
    origin_grid_m: float | None = None,
) -> tuple[int, int]:
    """就地填充各网格对某一品类的判定结果。

    返回 (直线剪枝判定数, 朴素点对数)：前者是零请求完成的网格 × 品类判定，
    后者是「每个网格对每个设施测一次」的朴素口径，两者一起量化省下的配额。

    origin_grid_m 给定时起点按这个细键缓存（50 米子格离父格心只有 25~35 米）。
    """
    name = category.name

    if name in coverage.failed:
        # 该品类的检索本身失败了，数量未知，不能推出任何网格结论
        for cell in cells:
            cell.nearest_m[name] = None
            cell.unknown.append(name)
        return 0, 0

    pois, excluded = available_pois(coverage.pois_of(name), closures)
    if check is not None:
        check.excluded_places += excluded
    if not pois:
        # 检索成功且确实一家都没有：采集半径已大于网格范围加判定阈值，可直接判定全域盲区
        for cell in cells:
            cell.nearest_m[name] = None
            cell.missing.append(name)
        return len(cells), 0

    ranked = {i: rank_candidates(cell, pois, cfg.walk_limit_m + 30) for i, cell in enumerate(cells)}
    pending: list[int] = []
    pruned = 0
    for i, cell in enumerate(cells):
        if ranked[i]:
            pending.append(i)
        else:
            # 最近设施已超出直线阈值及 30 米经验余量，按余量假设剪枝。
            cell.nearest_m[name] = None
            cell.missing.append(name)
            pruned += 1

    # 测距失败过的网格：即便最终未决也只能判"未知"，不能判盲区
    failed: set[int] = set()

    for round_no in range(cfg.max_rounds):
        if not pending:
            break

        # 把"第 round_no 近的候选相同"的网格并成一组：同组共用一个终点，
        # 矩阵退化成一列，点对数等于网格数，这是最省配额的形状
        groups: dict[int, list[int]] = {}
        exhausted: list[int] = []
        for ci in pending:
            if round_no < len(ranked[ci]):
                groups.setdefault(ranked[ci][round_no], []).append(ci)
            else:
                exhausted.append(ci)

        for ci in exhausted:
            # 阈值内的候选全测过且都走不到，是严格的盲区
            cell = cells[ci]
            if ci in failed:
                cell.unknown.append(name)
            else:
                cell.missing.append(name)

        pending = []

        async def measure(poi_idx: int, cell_indices: list[int], out: list[int]) -> None:
            """测一组「共用同一候选」的网格。各组的网格互不重叠，可以并发执行。

            有入口的设施一次测到它的每个入口（矩阵是「网格 × 入口」），取最近的一个。
            """
            poi = pois[poi_idx]
            dests = destinations(poi)
            # 入口终点用更细的缓存键：两个门相距二三十米时不能共用一个测距结果。
            # 没有入口时不传这个参数，测试里的假客户端与旧缓存都照旧
            fine: dict[str, float] = {"dest_grid_m": ENTRY_GRID_M} if poi.entries else {}
            if origin_grid_m:
                fine["origin_grid_m"] = origin_grid_m
            try:
                matrix = await client.walking_matrix_grid(
                    [(cells[ci].lat, cells[ci].lng) for ci in cell_indices], dests, **fine
                )
            except QuotaExhaustedError:
                # 配额在判定途中用完：这一组没测到，记为测距失败（最后判「未知」而不是盲区）。
                # 客户端已经熔断该接口，后面几轮的请求都会立即失败，不再白等重试
                matrix = [[None] * len(dests) for _ in cell_indices]
            for ci, row in zip(cell_indices, matrix, strict=True):
                cell = cells[ci]
                measured = [(e["distance_m"], j) for j, e in enumerate(row) if e is not None]
                if not measured:
                    failed.add(ci)
                    out.append(ci)
                    continue
                distance, best = min(measured)
                if distance > cfg.walk_limit_m:
                    known = cell.nearest_m.get(name)
                    if known is None or distance < known:
                        cell.nearest_m[name] = distance
                    if len(measured) < len(row):
                        # 有的门没测到：测到的门都走不到，不能断言这家走不到
                        failed.add(ci)
                    out.append(ci)  # 这家走不到，下一轮换更远的候选试
                    continue

                # 距离达标，但最短路线可能穿过围挡：按椭圆条件筛出需要核验的点对
                a = (cell.lat, cell.lng)
                b = dests[best]
                suspects = tuple(c for c in closures if route_may_touch(a, b, distance, c))
                if suspects:
                    if check is None or check.checked >= check.budget:
                        if check is not None:
                            check.unverified += 1
                        failed.add(ci)
                        out.append(ci)
                        continue
                    check.checked += 1  # 先占预算再等待，并发的组不会超额
                    hit = await _route_blocked(client, a, b, suspects, origin_grid_m)
                    if hit is None:
                        check.unverified += 1
                        failed.add(ci)
                        out.append(ci)
                        continue
                    if hit:
                        check.blocked += 1
                        out.append(ci)  # 最短路线被围挡挡住，换下一家候选
                        continue

                known = cell.nearest_m.get(name)
                if known is None or distance < known:
                    cell.nearest_m[name] = distance
                failed.discard(ci)  # 已找到达标设施，此前的失败不影响结论

        # 同一轮的各组并发发出，在途数与总速率由客户端的并发闸和令牌桶统一控制。
        # 按候选下标排序后提交，保证请求顺序（从而缓存与日志）可复现。
        await asyncio.gather(*(measure(p, groups[p], pending) for p in sorted(groups)))
        pending.sort()

    for ci in pending:
        cell = cells[ci]
        cell.nearest_m.setdefault(name, None)
        if ci not in failed and len(ranked[ci]) <= cfg.max_rounds:
            cell.missing.append(name)  # 候选恰好在最后一轮测完，仍是严格判定
        else:
            # 轮数用尽或测距失败：只能说"最近的几家走不到"，不能断言"一家都走不到"
            cell.unknown.append(name)

    # 朴素口径：每个网格对每家设施的每个终点（入口或坐标点）都测一次
    return pruned, len(cells) * sum(len(destinations(p)) for p in pois)


def scope_to_circle(blindspots: dict[str, Any] | None) -> dict[str, Any] | None:
    """把旧版（中心 1.5 公里圆形网格）的结果收成现版口径：只留 15 分钟圈内的格子。

    灰色区域、盲区占比与评分都只看圈内：体检的是这个生活圈，圈外的格子属于别的生活圈。
    旧结果里圈内格子的判定本身不变（同样是逐格路网实测），只是不再把圈外的算进来。
    现版结果原样返回。
    """
    if not blindspots or blindspots.get("layout") != "disc":
        return blindspots
    cells = [c for c in blindspots.get("cells") or [] if c.get("in_circle")]
    names = [
        *(blindspots.get("blind_ratio") or {}).keys(),
        *(blindspots.get("incomplete_categories") or []),
    ]
    ratio: dict[str, float] = {}
    incomplete: list[str] = []
    for name in names:
        judged = [c for c in cells if name not in (c.get("unknown") or [])]
        if judged and len(judged) >= BlindspotConfig.min_judged_share * len(cells):
            blind = sum(1 for c in judged if name in (c.get("missing") or []))
            ratio[name] = round(blind / len(judged), 3)
        else:
            incomplete.append(name)
    reach = [c["reach_s"] for c in cells if c.get("reach_s") is not None]
    return {
        **blindspots,
        "layout": "polygon",
        "scoped_from": "disc",
        "extent_m": None,
        "cells": cells,
        "cell_count": len(cells),
        "in_circle_count": len(cells),
        "blind_count": sum(1 for c in cells if c.get("missing")),
        "blind_ratio": ratio,
        "incomplete_categories": incomplete,
        "max_reach_s": max(reach) if reach else None,
    }


def layout_cells(
    center: tuple[float, float],
    polygon: list[tuple[float, float]],
    cfg: BlindspotConfig,
) -> list[CellResult]:
    if cfg.layout == "polygon":
        return [
            CellResult(lat=lat, lng=lng, in_circle=True)
            for lat, lng in grid_points(polygon, cfg.grid_spacing_m, cfg.grid_alignment)
        ]
    return [
        CellResult(
            lat=lat,
            lng=lng,
            in_circle=point_in_polygon(lat, lng, polygon) if has_polygon(polygon) else False,
        )
        for lat, lng in disc_cells(center, cfg.extent_m, cfg.grid_spacing_m)
    ]


async def identify_blindspots(
    client: BaiduMapClient,
    center: tuple[float, float],
    polygon: list[tuple[float, float]],
    coverage: CoverageResult,
    config: BlindspotConfig | None = None,
    categories: tuple[Category, ...] = KEY_CATEGORIES,
    refine: RefineResult | None = None,
    closures: tuple[Closure, ...] = (),
) -> BlindspotResult:
    cfg = config or BlindspotConfig()
    cells = layout_cells(center, polygon, cfg)
    if not cells:
        return BlindspotResult(center=center, cells=[], config=cfg, closures=closures)

    # 中心到各网格的步行耗时：单起点，步行一次请求最多 50 个终点，150 格约 3 次请求。
    # 终点按 10 米细键缓存，不复用同一 50 米缓存格里别的点的结果（见 REACH_GRID_M）
    try:
        rows = await client.walking_matrix_grid(
            [center], [(c.lat, c.lng) for c in cells], dest_grid_m=REACH_GRID_M
        )
        reach = rows[0] if rows else [None] * len(cells)
    except QuotaExhaustedError:
        # 热力是附加信息：配额耗尽时各格耗时记为未知，盲区判定照常尝试
        reach = [None] * len(cells)
    for cell, entry in zip(cells, reach, strict=True):
        if not entry:
            continue
        cell.reach_raw_s = entry["duration_s"]
        cell.reach_s = entry["duration_s"]
        cell.reach_m = entry["distance_m"]
        if refine is not None:
            delay, blocked, _ = refine.adjust_point(center, cell.lat, cell.lng, entry["distance_m"])
            cell.reach_s = entry["duration_s"] + delay
            if blocked:
                cell.closure_blocked = True
                cell.reach_s = None

    check = ClosureCheck(budget=cfg.max_closure_checks) if closures else None

    # 品类之间串行：并发会让多个大矩阵同时挤令牌桶，反而更容易撞上并发限流
    total_pruned = 0
    total_naive = 0
    for category in categories:
        pruned, naive = await _judge_category(
            client, cells, category, coverage, cfg, closures, check
        )
        total_pruned += pruned
        total_naive += naive

    refinement = None
    if cfg.refine_boundary and cfg.layout == "polygon" and len(cells) > 1:
        refinement = await _refine(
            client, cells, categories, coverage, cfg, closures, check, polygon
        )

    # 占比按面积计：细分过的格由圈内子格（各占 1/4）替代，不和父格重复统计
    units = area_units(cells)
    blind_ratio: dict[str, float] = {}
    incomplete: list[str] = []
    for category in categories:
        judged = sum(w for u, w in units if category.name not in u.unknown)
        if judged > 0 and judged >= cfg.min_judged_share * sum(w for _, w in units):
            blind = sum(
                w for u, w in units if category.name not in u.unknown and category.name in u.missing
            )
            blind_ratio[category.name] = blind / judged
        elif category.name not in coverage.failed:
            # 检索失败的品类已在覆盖层单独说明，这里只记「测了但没测够」
            incomplete.append(category.name)

    return BlindspotResult(
        center=center,
        cells=cells,
        config=cfg,
        blind_ratio=blind_ratio,
        pruned_decisions=total_pruned,
        naive_matrix_pairs=total_naive,
        delay_applied=bool(refine and refine.delay_applied),
        closures=closures,
        closure_check=check,
        incomplete_categories=incomplete,
        refinement=refinement,
    )


QUARTERS = (("NW", 1, -1), ("NE", 1, 1), ("SW", -1, -1), ("SE", -1, 1))


def _quarters_of(
    cell: CellResult, half_m: float, mpl: float, polygon: list[tuple[float, float]]
) -> list[CellResult]:
    """切成 4 个子格，先整体继承父格的结论；实测过的品类再逐个替换。

    子格中心是否在圈内按子格自己判（与 100 米格「格心在圈内才算」同一条规则）：
    父格心在圈内、子格中心在圈外的，不测、不判定，也不进任何计数。
    """
    dlat = half_m / METERS_PER_DEG_LAT
    dlng = half_m / mpl
    out = []
    for label, sy, sx in QUARTERS:
        lat, lng = cell.lat + sy * dlat, cell.lng + sx * dlng
        inside = point_in_polygon(lat, lng, polygon) if has_polygon(polygon) else cell.in_circle
        out.append(
            CellResult(
                lat=lat,
                lng=lng,
                nearest_m=dict(cell.nearest_m) if inside else {},
                missing=list(cell.missing) if inside else [],
                unknown=list(cell.unknown) if inside else [],
                unknown_reasons=dict(cell.unknown_reasons) if inside else {},
                in_circle=inside,
                q=label,
            )
        )
    return out


class _PairGuard:
    """细分专用的点对闸：每批请求发出前按缓存缺口逐个起点算要花多少新点对，放不下的起点不发。

    只拦放不下的起点（子格），被拦过的子格在这一品类后面的请求里直接剔掉：它的结论反正要
    退回继承父格，再测就是白花点对。放得下的子格照常测完，剩下的额度不浪费。
    计数只看经过这道闸的请求，与进程里别的分析同时花的点对无关；假客户端没有缓存时
    每个点对都按新增计。路线规划（围挡核验）原样转给客户端，它有自己的预算。
    """

    def __init__(self, client: Any, budget: int) -> None:
        self._client = client
        self.budget = budget
        self.used = 0
        self.requested = 0
        self.refused: set[tuple[float, float]] = set()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def _cost(self, origins, destinations, dest_grid_m, origin_grid_m) -> int:
        probe = getattr(self._client, "missing_pairs", None)
        if probe is None:
            return len(origins) * len(destinations)
        return int(
            probe(
                "walk",
                origins,
                destinations,
                dest_grid_m=dest_grid_m,
                origin_grid_m=origin_grid_m,
            )
        )

    async def walking_matrix_grid(
        self, origins, destinations, dest_grid_m=None, origin_grid_m=None
    ) -> list[list[dict[str, float] | None]]:
        out: list[list[dict[str, float] | None]] = [[None] * len(destinations) for _ in origins]
        keep: list[int] = []
        for i, o in enumerate(origins):
            at = (o[0], o[1])
            if at in self.refused:
                continue
            # 逐个起点算成本：一批放不下时，放得下的起点照常测，剩下的才拦
            cost = self._cost([o], destinations, dest_grid_m, origin_grid_m)
            if cost and self.used + cost > self.budget:
                self.refused.add(at)  # 这一品类没测完：调用方把它退回继承父格
                continue
            self.used += cost
            keep.append(i)
        if not keep:
            return out
        sub = [origins[i] for i in keep]
        self.requested += len(sub) * len(destinations)
        kwargs: dict[str, float] = {}
        if dest_grid_m is not None:
            kwargs["dest_grid_m"] = dest_grid_m
        if origin_grid_m is not None:
            kwargs["origin_grid_m"] = origin_grid_m
        rows = await self._client.walking_matrix_grid(sub, destinations, **kwargs)
        for i, row in zip(keep, rows, strict=True):
            out[i] = row
        return out


async def _refine(
    client: BaiduMapClient,
    cells: list[CellResult],
    categories: tuple[Category, ...],
    coverage: CoverageResult,
    cfg: BlindspotConfig,
    closures: tuple[Closure, ...],
    check: ClosureCheck | None,
    polygon: list[tuple[float, float]] | None = None,
) -> Refinement:
    """50 米叠加细分：每个品类的边界格与临界格切成 4 个子格，用同一套判定分别实测。

    新增点对不超过 cfg.refine_max_pairs：判定照常分轮发请求，每批都先过 _PairGuard。
    被拦下的子格这一品类保留继承来的父格结论，记进 fallback；一个品类都没测成的父格撤掉子格。
    """
    spacing = cfg.grid_spacing_m
    ref = Refinement(spacing_m=spacing / 2, max_pairs=cfg.refine_max_pairs)
    lat0 = min(c.lat for c in cells)
    lng0 = min(c.lng for c in cells)
    mean_lat = sum(c.lat for c in cells) / len(cells)
    mpl = meters_per_deg_lng(mean_lat)
    dlat, dlng = spacing / METERS_PER_DEG_LAT, spacing / mpl
    index = {(round((c.lat - lat0) / dlat), round((c.lng - lng0) / dlng)): c for c in cells}
    guard = _PairGuard(client, cfg.refine_max_pairs)
    ring = polygon or []

    for category in categories:
        name = category.name
        if name in coverage.failed:
            continue
        targets = []
        for (y, x), c in index.items():
            if name in c.unknown:
                continue
            here = name in c.missing
            boundary = any(
                nb is not None and name not in nb.unknown and (name in nb.missing) != here
                for nb in (
                    index.get((y + dy, x + dx)) for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1))
                )
            )
            near = c.nearest_m.get(name)
            critical = near is not None and abs(near - cfg.walk_limit_m) <= cfg.refine_critical_m
            if boundary or critical:
                targets.append(c)
        if not targets:
            continue
        targets.sort(key=lambda c: (c.lat, c.lng))
        split: list[CellResult] = []
        for c in targets:
            if c.quarters is None:
                quarters = _quarters_of(c, spacing / 4, mpl, ring)
                if not any(q.in_circle for q in quarters):
                    continue  # 4 个子格中心都在圈外（极窄的圈边）：保持 100 米格
                c.quarters = quarters
            split.append(c)
        subs = [(c, q) for c in split for q in c.quarters or [] if q.in_circle]
        if not subs:
            continue
        probes = [CellResult(lat=q.lat, lng=q.lng) for _, q in subs]
        guard.refused.clear()  # 按品类各自计：别的品类被拦过的子格，这一品类照样能测
        await _judge_category(
            guard, probes, category, coverage, cfg, closures, check, origin_grid_m=REACH_GRID_M
        )
        fallback = 0
        refined: set[int] = set()
        for (c, q), p in zip(subs, probes, strict=True):
            if (p.lat, p.lng) in guard.refused:
                fallback += 1  # 预算拦下了这个子格的请求：这一品类照旧继承父格
                continue
            q.missing = [m for m in q.missing if m != name] + ([name] if name in p.missing else [])
            q.unknown = [u for u in q.unknown if u != name] + ([name] if name in p.unknown else [])
            q.nearest_m[name] = p.nearest_m.get(name)
            q.unknown_reasons.pop(name, None)
            if name in p.unknown_reasons:
                q.unknown_reasons[name] = p.unknown_reasons[name]
            q.measured.append(name)
            refined.add(id(c))
        if fallback:
            ref.fallback[name] = fallback
            ref.stopped.append(name)
        if refined:
            ref.parents[name] = len(refined)
    # 一个品类都没测成的格不算细分过：撤掉子格，照旧按一整格计
    for c in cells:
        if c.quarters and not any(q.measured for q in c.quarters):
            c.quarters = None
    ref.pairs = guard.used
    ref.requested_pairs = guard.requested
    return ref
