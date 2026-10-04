"""网格级服务盲区识别（真实路网口径）。

评分细则要求"识别出周边 1 公里内没有菜市场、药店或小学的服务盲区点位"。
品类计数只能回答"整个圈内有没有"，回答不了"哪一片居民走不到"——
一个圈内有两家药店的社区，被铁路切开的另一侧照样是盲区。故按网格逐点判定：

1. 在 15 分钟步行圈内按 100 米铺网格（曹杨约 144 格、桃浦约 116 格）。体检的是这个生活圈，
   灰色区域只标圈内；但设施照样检索到圈外 1 公里，圈边上的居民走到圈外的药店也算有；
2. 中心点到各网格做一次批量算路，得到步行耗时，即热力图的强度；
   步行时再用等时圈各方向的路线剖面补回过街等待（见 isochrone/refine.py）；
3. 对每个关键品类，为各网格预筛若干直线最近的候选设施，
   用**多起点批量算路**求真实路网步行距离；
4. 步行 1 公里内一家都到不了（或该品类附近无设施）的网格，标为该品类的盲区点位。

**直线距离只做数学下界，从不用来判定「够得着」。** 直线就超过 1 公里的设施，
步行必然超过，据此免费剪枝；而直线 1 公里内的设施，必须实测步行距离才能下结论。

配额是这里的主要设计约束：批量算路的日额度按点对计量，
朴素做法（每个网格对每个设施测距）的点对数是网格数 × 设施数 × 品类数，
很快就会耗尽。三条措施把它压下来，且都不牺牲结论的严格性：

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
from dataclasses import dataclass, field
from typing import Any

from ..baidu.client import BaiduMapClient
from ..baidu.errors import QuotaExhaustedError
from ..isochrone.geometry import (
    disc_cells,
    first_entry_along,
    grid_points,
    haversine_m,
    point_in_polygon,
)
from ..isochrone.refine import Closure, RefineResult
from ..poi.catalog import KEY_CATEGORIES, Category
from ..poi.collect import CoverageResult, Poi
from ..poi.entries import ENTRY_GRID_M, destinations, straight_m


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


@dataclass
class CellResult:
    lat: float
    lng: float
    # 中心点步行到该网格的耗时（已补回过街等待），热力图强度由它决定。
    # None 表示测距失败或受围挡阻断。
    reach_s: float | None = None
    # 未补过街等待的批量算路耗时
    reach_raw_s: float | None = None
    # 品类 -> 最近设施的真实路网步行距离（米）。None 表示无法判定。
    nearest_m: dict[str, float | None] = field(default_factory=dict)
    # 1 公里内确无此类设施的品类
    missing: list[str] = field(default_factory=list)
    # 测距失败、既不能判有也不能判无的品类
    unknown: list[str] = field(default_factory=list)
    # 是否落在 15 分钟等时圈内
    in_circle: bool = True
    # 从中心过来的路线被施工围挡挡住
    closure_blocked: bool = False

    @property
    def is_blind(self) -> bool:
        return bool(self.missing)

    def as_dict(self) -> dict[str, Any]:
        return {
            "lat": round(self.lat, 6),
            "lng": round(self.lng, 6),
            "reach_s": round(self.reach_s) if self.reach_s is not None else None,
            "reach_raw_s": round(self.reach_raw_s) if self.reach_raw_s is not None else None,
            "nearest_m": {
                k: (round(v) if v is not None else None) for k, v in self.nearest_m.items()
            },
            "missing": self.missing,
            "unknown": self.unknown,
            "in_circle": self.in_circle,
            "closure_blocked": self.closure_blocked,
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

    @property
    def blind_cells(self) -> list[CellResult]:
        return [c for c in self.cells if c.is_blind]

    def as_dict(self) -> dict[str, Any]:
        cells = self.cells
        max_reach = max((c.reach_s for c in cells if c.reach_s is not None), default=None)
        return {
            "basis": "network",
            "layout": self.config.layout,
            "grid_spacing_m": self.config.grid_spacing_m,
            "extent_m": self.config.extent_m if self.config.layout == "disc" else None,
            "walk_limit_m": self.config.walk_limit_m,
            "cell_count": len(cells),
            "blind_count": len(self.blind_cells),
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
        }


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
) -> bool | None:
    """取一条步行路线核验是否穿过围挡。None 表示路线取不到、无法核验。"""
    route = await client.walking_route(a, b)
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
    client: BaiduMapClient,
    cells: list[CellResult],
    category: Category,
    coverage: CoverageResult,
    cfg: BlindspotConfig,
    closures: tuple[Closure, ...] = (),
    check: ClosureCheck | None = None,
) -> tuple[int, int]:
    """就地填充各网格对某一品类的判定结果。

    返回 (直线剪枝判定数, 朴素点对数)：前者是零请求完成的网格 × 品类判定，
    后者是「每个网格对每个设施测一次」的朴素口径，两者一起量化省下的配额。
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

    ranked = {i: rank_candidates(cell, pois, cfg.walk_limit_m) for i, cell in enumerate(cells)}
    pending: list[int] = []
    pruned = 0
    for i, cell in enumerate(cells):
        if ranked[i]:
            pending.append(i)
        else:
            # 连最近的设施直线距离都超标，步行只会更远，无需测距
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
                    hit = await _route_blocked(client, a, b, suspects)
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
            for lat, lng in grid_points(polygon, cfg.grid_spacing_m)
        ]
    return [
        CellResult(
            lat=lat,
            lng=lng,
            in_circle=point_in_polygon(lat, lng, polygon) if len(polygon) >= 3 else False,
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

    # 中心到各网格的步行耗时：单起点，一次请求可带 100 个终点，150 格以内只需 2 次请求
    try:
        reach = await client.walking_matrix(center, [(c.lat, c.lng) for c in cells])
    except QuotaExhaustedError:
        # 热力是附加信息：配额耗尽时各格耗时记为未知，盲区判定照常尝试
        reach = [None] * len(cells)
    for cell, entry in zip(cells, reach, strict=True):
        if not entry:
            continue
        cell.reach_raw_s = entry["duration_s"]
        cell.reach_s = entry["duration_s"]
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

    blind_ratio: dict[str, float] = {}
    incomplete: list[str] = []
    for category in categories:
        judged = [c for c in cells if category.name not in c.unknown]
        if judged and len(judged) >= cfg.min_judged_share * len(cells):
            blind = sum(1 for c in judged if category.name in c.missing)
            blind_ratio[category.name] = blind / len(judged)
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
    )
