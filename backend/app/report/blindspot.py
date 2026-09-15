"""网格级服务盲区识别。

评分细则要求"识别出周边 1 公里内没有菜市场、药店或小学的服务盲区点位"。
品类计数只能回答"整个圈内有没有"，回答不了"哪一片居民走不到"——
一个圈内有两家药店的社区，被铁路切开的另一侧照样是盲区。故按网格逐点判定：

1. 在等时圈内按固定间距布网格点；
2. 中心点到各网格做一次批量算路，得到步行耗时，即热力图的强度；
3. 对每个关键品类，为各网格预筛若干直线最近的候选设施，
   用**多起点批量算路**求真实路网最近距离；
4. 最近距离超过 1 公里（或该品类圈内无设施）的网格，标为该品类的盲区点位。

两个关键取舍：

- **候选集用直线预筛**。理论上应对全部设施测距，但网格数 × 设施数会让请求量失控。
  直线最近的几家里通常包含路网最近的那家；被河道隔开的近点会被算路如实排除，
  由候选集里更远的一家接管。代价是极端情形（最近的几家全被屏障隔断、真正可达的
  那家在直线上排在后面）会把网格误判成盲区，属于偏保守的方向。
- **全部候选都测不出距离时判为"未知"而非"盲区"**。批量算路对不可达点与请求失败
  都返回空值，两者无法区分。既然本项目的核心输出就是"这里缺设施"，
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

    # 每个网格为每个品类预筛的直线最近候选数。
    candidates_per_cell: int = 2

    # 每个品类的候选集上限。候选集是所有网格预筛结果的并集，
    # 不设上限会在设施密集区退化成"全量测距"。
    #
    # 这个数直接决定配额消耗：点对数 = 网格数 × 候选数 × 关键品类数，
    # 按乘积 100 装箱后即为请求数。78 个网格 × 8 个候选 × 3 类约 19 次请求。
    # 批量算路的日配额并未随地点检索一同提升，这里必须克制。
    max_candidates: int = 8


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
        }


def _pick_candidates(
    cells: list[CellResult], pois: list[Poi], cfg: BlindspotConfig
) -> list[Poi]:
    """取各网格直线最近的若干设施之并集，按被选中次数截断到上限。"""
    if not pois:
        return []

    votes: dict[int, int] = {}
    for cell in cells:
        ranked = sorted(
            range(len(pois)),
            key=lambda i: haversine_m(cell.lat, cell.lng, pois[i].lat, pois[i].lng),
        )
        for idx in ranked[: cfg.candidates_per_cell]:
            votes[idx] = votes.get(idx, 0) + 1

    ordered = sorted(votes, key=lambda i: votes[i], reverse=True)
    return [pois[i] for i in ordered[: cfg.max_candidates]]


async def _judge_category(
    client: BaiduMapClient,
    cells: list[CellResult],
    category: Category,
    coverage: CoverageResult,
    cfg: BlindspotConfig,
) -> None:
    """就地填充各网格对某一品类的判定结果。"""
    if category.name in coverage.failed:
        # 该品类的检索本身失败了，数量未知，不能推出任何网格结论
        for cell in cells:
            cell.nearest_m[category.name] = None
            cell.unknown.append(category.name)
        return

    pois = coverage.pois_of(category.name)
    if not pois:
        # 检索成功且确实一家都没有：采集半径已大于等时圈，可直接判定全域盲区
        for cell in cells:
            cell.nearest_m[category.name] = None
            cell.missing.append(category.name)
        return

    candidates = _pick_candidates(cells, pois, cfg)
    matrix = await client.walking_matrix_grid(
        [(c.lat, c.lng) for c in cells],
        [(p.lat, p.lng) for p in candidates],
    )

    for cell, row in zip(cells, matrix, strict=True):
        distances = [e["distance_m"] for e in row if e is not None]
        if not distances:
            cell.nearest_m[category.name] = None
            cell.unknown.append(category.name)
            continue
        nearest = min(distances)
        cell.nearest_m[category.name] = nearest
        if nearest > cfg.walk_limit_m:
            cell.missing.append(category.name)


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
    for category in categories:
        await _judge_category(client, cells, category, coverage, cfg)

    blind_ratio: dict[str, float] = {}
    for category in categories:
        judged = [c for c in cells if category.name not in c.unknown]
        if judged:
            blind = sum(1 for c in judged if category.name in c.missing)
            blind_ratio[category.name] = blind / len(judged)

    return BlindspotResult(center=center, cells=cells, config=cfg, blind_ratio=blind_ratio)
