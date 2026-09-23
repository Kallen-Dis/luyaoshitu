"""离线模拟数据生成——零 AK、零 API 消耗的保底演示路径。

当 AK 未配置、配额耗尽或断网时，真实分析走不通；本模块用**确定性伪随机**
（seed 取自中心点坐标）生成一套结构完整、可重复的模拟体检结果，
UI、算法链路、评分、处方、导出全部照常运转。

铁律：所有输出明确标注 `simulated`，页面与文案都不允许把它
冒充成真实路网结论。同一坐标重复生成结果完全一致（可复现演示）。
"""

from __future__ import annotations

import math
import random
from typing import Any

from .isochrone.geometry import (
    grid_points,
    haversine_m,
    offset_point,
    polygon_area_m2,
    smooth_radii,
)
from .poi.catalog import CATEGORIES, KEY_CATEGORIES
from .report.blindspot import BlindspotConfig
from .report.score import WALK_SPEED_M_PER_S, build_report

GRID_SPACING_M = 150.0


def _seed(lat: float, lng: float) -> int:
    """坐标 → 稳定种子。同一中心点每次演示结果一致。"""
    return int(abs(hash((round(lat, 4), round(lng, 4)))) % (2**32))


def build_demo_feature(
    lat: float,
    lng: float,
    minutes: float = 15.0,
    directions: int = 36,
    mode_id: str = "walk",
) -> dict[str, Any]:
    """生成一份完整的模拟 Feature，结构与实时 /api/isochrone 响应一致。"""
    rng = random.Random(_seed(lat, lng))
    speed = WALK_SPEED_M_PER_S
    base_m = minutes * 60.0 * speed

    # 等时圈：各方向半径在基准值上做 0.45~1.0 的确定性扰动，
    # 再平滑出连续轮廓，观感接近真实采样插值结果。
    raw_radii = [base_m * (0.45 + 0.55 * rng.random()) for _ in range(directions)]
    radii = smooth_radii(raw_radii, 3)
    polygon = [
        offset_point(lat, lng, 360.0 * d / directions, r)
        for d, r in enumerate(radii)
    ]
    area_m2 = polygon_area_m2(polygon)
    positive = [r for r in radii if r > 0] or [0.0]
    mean_detour = round(1.05 + 0.35 * rng.random(), 3)

    # 设施覆盖：六品类确定性计数；随机挑一个关键品类设为 0 制造盲区，
    # 且让它"附近有、圈内无"——这正是本项目要讲的直线法误判故事。
    blind_cat = rng.choice(KEY_CATEGORIES).name
    categories: dict[str, int] = {}
    nearby: dict[str, int] = {}
    for c in CATEGORIES:
        n = rng.randint(2, 12) if c.name != "基础教育" else rng.randint(0, 4)
        categories[c.name] = n
        nearby[c.name] = n + rng.randint(0, 5)
    categories[blind_cat] = 0
    nearby[blind_cat] = rng.randint(2, 6)

    # 网格盲区：边缘网格更可能走不到（模拟屏障切割的局部性）。
    cells: list[dict[str, Any]] = []
    max_r = max(positive)
    for i, (clat, clng) in enumerate(grid_points(polygon, GRID_SPACING_M)):
        dist = haversine_m(lat, lng, clat, clng)
        cell_rng = random.Random(_seed(lat, lng) + i + 1)
        nearest: dict[str, float | None] = {}
        missing: list[str] = []
        for kc in KEY_CATEGORIES:
            if categories[kc.name] == 0:
                nearest[kc.name] = None
                missing.append(kc.name)
            else:
                d = round(cell_rng.uniform(250, 1500))
                nearest[kc.name] = d
                if d > 1000 and cell_rng.random() < 0.35 + 0.55 * (dist / max_r):
                    missing.append(kc.name)
        cells.append(
            {
                "lat": round(clat, 6),
                "lng": round(clng, 6),
                "reach_s": round(dist / speed),
                "nearest_m": nearest,
                "missing": missing,
                "unknown": [],
            }
        )

    blind_count = sum(1 for c in cells if c["missing"])
    blind_ratio: dict[str, float] = {}
    for kc in KEY_CATEGORIES:
        judged = len(cells)
        if judged:
            miss = sum(1 for c in cells if kc.name in c["missing"])
            blind_ratio[kc.name] = round(miss / judged, 3)

    coverage = {
        "categories": categories,
        "nearby_categories": nearby,
        "failed_categories": [],
        "searches": 0,
        "radius_m": 1800,
        "clean_stats": {},
        "source": "离线模拟数据（非真实 API）",
    }
    blindspots = {
        "grid_spacing_m": GRID_SPACING_M,
        "walk_limit_m": BlindspotConfig().walk_limit_m,
        "cell_count": len(cells),
        "blind_count": blind_count,
        "max_reach_s": max((c["reach_s"] for c in cells), default=None),
        "blind_ratio": blind_ratio,
        "cells": cells,
        "pruned_decisions": 0,
        "naive_matrix_pairs": 0,
    }

    props: dict[str, Any] = {
        "name": f"【模拟】自定义中心 ({lat:.4f}, {lng:.4f})",
        "minutes": float(minutes),
        "area_m2": round(area_m2, 1),
        "area_km2": round(area_m2 / 1e6, 4),
        "mean_radius_m": round(sum(radii) / len(radii), 1),
        "min_radius_m": round(min(positive), 1),
        "max_radius_m": round(max(positive), 1),
        "compactness": round(min(positive) / max(positive), 3) if max(positive) else 0.0,
        "mean_detour": mean_detour,
        "max_detour": round(mean_detour + 0.5 * rng.random(), 3),
        "sampled_points": directions * 7,
        "failed_points": 0,
        "mode": mode_id,
        "mode_label": "步行",
        "uses_traffic": False,
        "speed_m_per_s": speed,
        "factors": [
            "离线模拟：形状由确定性伪随机生成，不代表真实路网",
        ],
        "rays": [
            {
                "bearing": round(360.0 * d / directions, 1),
                "radius_m": round(r, 1),
                "detour": mean_detour,
                "barrier": False,
            }
            for d, r in enumerate(radii)
        ],
        "quality": {
            "directions": directions,
            "saturated": 0,
            "barrier_truncated": 0,
            "zero_radius": 0,
        },
        "center": {"lat": round(lat, 6), "lng": round(lng, 6)},
        "coverage": coverage,
        "blindspots": blindspots,
        "simulated": True,
        "generated_at": None,
    }
    props["report"] = build_report(props, coverage, blindspots)
    ring = [[lng, lat] for lat, lng in polygon]
    if ring and ring[0] != ring[-1]:
        ring.append(ring[0])
    return {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [ring],
        },
        "properties": props,
    }
