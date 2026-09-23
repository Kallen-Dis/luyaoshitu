"""网格级服务盲区识别。

评分细则要求"识别出周边 1 公里内没有菜市场、药店或小学的服务盲区点位"。
品类计数只能回答"整个圈内有没有"，回答不了"哪一片居民走不到"——
一个圈内有两家药店的社区，被铁路切开的另一侧照样是盲区。故按网格逐点判定：

1. 在等时圈内按固定间距布网格点；
2. 中心点到各网格做一次批量算路，得到步行耗时，即热力图的强度；
3. 对每个关键品类，为各网格预筛若干直线最近的候选设施，
   用**多起点批量算路**求真实路网最近距离；
4. 最近距离超过 1 公里（或该品类圈内无设施）的网格，标为该品类的盲区点位。

配额是这里的主要设计约束：批量算路的日额度并未随地点检索一同提升，
而朴素做法（每个网格对每个设施测距）的点对数是网格数 × 设施数 × 品类数，
很快就会耗尽。三条措施把它压下来，且都不牺牲结论的严格性：

1. **直线距离是步行距离的下界，据此免费剪枝**。直线距离就超过 1 公里的设施，
   步行必然超过。因此某网格若连最近的设施直线距离都超标，无需任何请求即可
   判定为盲区；候选集也只保留直线距离在阈值内的设施。桃浦镇最近的菜市场
   直线 1091 米，这一整类因此零消耗。
2. **按最近候选分组，逐轮测距，够近就停**。判定"1 公里内有没有"只需找到一个
   达标设施，不必求真正的最近距离。每轮给未决网格各分配"第 k 近"的候选，
   把共用同一候选的网格并成一次请求（矩阵退化成一列，点对数等于网格数）。
   多数网格在第一轮就被最近的设施覆盖掉。
3. **候选耗尽即可判定**。某网格把直线阈值内的候选全测完仍无一达标，
   就是严格的盲区——阈值外的设施步行只会更远。

另一条不容妥协的取舍：**测距失败判为"未知"而非"盲区"**。批量算路对不可达点与
请求失败都返回空值，两者无法区分。既然本项目的核心输出就是"这里缺设施"，
就不能让一次接口抖动伪装成盲区。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..baidu.client import BaiduMapClient
from ..isochrone.geometry import grid_points, haversine_m
from ..poi.catalog import KEY_CATEGORIES, Category
from ..poi.collect import CoverageResult, Poi


@dataclass(frozen=True)
class BlindspotConfig:
    # 网格间距。150 米在 1.3~1.8 km² 的等时圈里约产生 60~80 个网格，
    # 配合每网格每品类 2 个候选，总点对数落在批量算路的可承受区间。
    grid_spacing_m: float = 150.0

    # 命题给定的盲区判定阈值：步行 1 公里内无此类设施。
    walk_limit_m: float = 1000.0

    # 每个网格最多测几轮候选。一轮 = 给未决网格各分配"第 k 近"的候选并测距。
    #
    # 达到上限仍未决的网格判为"未知"而不是盲区：此时只能说"最近的几家走不到"，
    # 不能断言"一家都走不到"。设 4 是因为直线阈值内候选通常不足 4 家，
    # 实际多在候选耗尽时就已严格判定，很少触到这个上限。
    max_rounds: int = 4


@dataclass
class CellResult:
    lat: float
    lng: float
    # 中心点步行到该网格的耗时，热力图强度由它决定。None 表示测距失败。
    reach_s: float | None = None
    # 品类 -> 最近设施的真实路网步行距离（米）。None 表示无法判定。
    nearest_m: dict[str, float | None] = field(default_factory=dict)
    # 1 公里内确无此类设施的品类
    missing: list[str] = field(default_factory=list)
    # 测距失败、既不能判有也不能判无的品类
    unknown: list[str] = field(default_factory=list)

    @property
    def is_blind(self) -> bool:
        return bool(self.missing)

    def as_dict(self) -> dict[str, Any]:
        return {
            "lat": round(self.lat, 6),
            "lng": round(self.lng, 6),
            "reach_s": round(self.reach_s) if self.reach_s is not None else None,
            "nearest_m": {
                k: (round(v) if v is not None else None) for k, v in self.nearest_m.items()
            },
            "missing": self.missing,
            "unknown": self.unknown,
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
    # 是「配额优化省下多少」最直观的口径。值为 None 表示未做盲区判定。
    naive_matrix_pairs: int = 0

    @property
    def blind_cells(self) -> list[CellResult]:
        return [c for c in self.cells if c.is_blind]

    def as_dict(self) -> dict[str, Any]:
        cells = self.cells
        max_reach = max((c.reach_s for c in cells if c.reach_s is not None), default=None)
        return {
            "grid_spacing_m": self.config.grid_spacing_m,
            "walk_limit_m": self.config.walk_limit_m,
            "cell_count": len(cells),
            "blind_count": len(self.blind_cells),
            "max_reach_s": round(max_reach) if max_reach is not None else None,
            "blind_ratio": {k: round(v, 3) for k, v in self.blind_ratio.items()},
            "cells": [c.as_dict() for c in cells],
            "pruned_decisions": self.pruned_decisions,
            "naive_matrix_pairs": self.naive_matrix_pairs,
        }


def rank_candidates(
    cell: CellResult, pois: list[Poi], limit_m: float
) -> list[int]:
    """按直线距离给某网格排出候选设施，只保留直线距离在阈值内的。

    直线距离是步行距离的下界，故直线就超标的设施不可能在阈值内走到，
    连请求都不必发。返回的是 pois 的下标，按由近及远排列。
    """
    within = [
        i
        for i, p in enumerate(pois)
        if haversine_m(cell.lat, cell.lng, p.lat, p.lng) <= limit_m
    ]
    return sorted(
        within, key=lambda i: haversine_m(cell.lat, cell.lng, pois[i].lat, pois[i].lng)
    )


async def _judge_category(
    client: BaiduMapClient,
    cells: list[CellResult],
    category: Category,
    coverage: CoverageResult,
    cfg: BlindspotConfig,
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

    pois = coverage.pois_of(name)
    if not pois:
        # 检索成功且确实一家都没有：采集半径已大于等时圈，可直接判定全域盲区
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
        for poi_idx, cell_indices in groups.items():
            poi = pois[poi_idx]
            matrix = await client.walking_matrix_grid(
                [(cells[ci].lat, cells[ci].lng) for ci in cell_indices],
                [(poi.lat, poi.lng)],
            )
            for ci, row in zip(cell_indices, matrix, strict=True):
                cell = cells[ci]
                entry = row[0]
                if entry is None:
                    failed.add(ci)
                    pending.append(ci)
                    continue
                distance = entry["distance_m"]
                known = cell.nearest_m.get(name)
                if known is None or distance < known:
                    cell.nearest_m[name] = distance
                if distance > cfg.walk_limit_m:
                    pending.append(ci)  # 这家走不到，下一轮换更远的候选试
                else:
                    failed.discard(ci)  # 已找到达标设施，此前的失败不影响结论

    for ci in pending:
        cell = cells[ci]
        cell.nearest_m.setdefault(name, None)
        if ci not in failed and len(ranked[ci]) <= cfg.max_rounds:
            cell.missing.append(name)  # 候选恰好在最后一轮测完，仍是严格判定
        else:
            # 轮数用尽或测距失败：只能说"最近的几家走不到"，不能断言"一家都走不到"
            cell.unknown.append(name)

    return pruned, len(cells) * len(pois)


async def identify_blindspots(
    client: BaiduMapClient,
    center: tuple[float, float],
    polygon: list[tuple[float, float]],
    coverage: CoverageResult,
    config: BlindspotConfig | None = None,
    categories: tuple[Category, ...] = KEY_CATEGORIES,
) -> BlindspotResult:
    cfg = config or BlindspotConfig()
    cells = [
        CellResult(lat=lat, lng=lng)
        for lat, lng in grid_points(polygon, cfg.grid_spacing_m)
    ]
    if not cells:
        return BlindspotResult(center=center, cells=[], config=cfg)

    # 中心到各网格的步行耗时：单起点，一次请求可带 100 个终点，热力图几乎白送
    reach = await client.walking_matrix(center, [(c.lat, c.lng) for c in cells])
    for cell, entry in zip(cells, reach, strict=True):
        cell.reach_s = entry["duration_s"] if entry else None

    # 品类之间串行：并发会让多个大矩阵同时挤令牌桶，反而更容易撞上并发限流
    total_pruned = 0
    total_naive = 0
    for category in categories:
        pruned, naive = await _judge_category(client, cells, category, coverage, cfg)
        total_pruned += pruned
        total_naive += naive

    blind_ratio: dict[str, float] = {}
    for category in categories:
        judged = [c for c in cells if category.name not in c.unknown]
        if judged:
            blind = sum(1 for c in judged if category.name in c.missing)
            blind_ratio[category.name] = blind / len(judged)

    return BlindspotResult(
        center=center,
        cells=cells,
        config=cfg,
        blind_ratio=blind_ratio,
        pruned_decisions=total_pruned,
        naive_matrix_pairs=total_naive,
    )
