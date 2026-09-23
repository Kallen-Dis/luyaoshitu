"""与前端 wideBlind.ts 同一套方格。

中心周围 1.5 公里、200 米一格，硬指标按直线 1 公里判定。
地图、右侧体检和模拟新建都读这里，避免分数说的格子和画面上的红格不是一回事。
改格子尺寸或判定距离时，两边要一起改。
"""

from __future__ import annotations

import math
from typing import Any

from ..isochrone.geometry import haversine_m, point_in_polygon

CELL_M = 200.0
EXTENT_M = 1500.0
RADIUS_M = 1000.0
KEYS = ("生鲜采买", "医药", "基础教育")


def polygon_of(feature: dict[str, Any] | None) -> list[tuple[float, float]] | None:
    """GeoJSON 外环转成 (lat, lng)。"""
    if not feature:
        return None
    coords = (feature.get("geometry") or {}).get("coordinates") or []
    if not coords or not coords[0]:
        return None
    return [(float(p[1]), float(p[0])) for p in coords[0] if len(p) >= 2]


def _offset(lat: float, lng: float, east_m: float, north_m: float) -> tuple[float, float]:
    d_lat = north_m / 111_320.0
    d_lng = east_m / (111_320.0 * math.cos(math.radians(lat)))
    return lat + d_lat, lng + d_lng


def wide_grid(
    center: dict[str, float],
    places: list[dict[str, Any]],
    polygon: list[tuple[float, float]],
) -> dict[str, Any]:
    """返回与地图红格一致的盲区统计，供评分和处方使用。"""
    lat0 = float(center["lat"])
    lng0 = float(center["lng"])
    buckets: dict[str, list[tuple[float, float]]] = {key: [] for key in KEYS}
    for place in places:
        key = place.get("category")
        if key in buckets and place.get("lat") is not None and place.get("lng") is not None:
            buckets[key].append((float(place["lat"]), float(place["lng"])))

    n = math.floor(EXTENT_M / CELL_M)
    cells: list[dict[str, Any]] = []
    total = 0
    missing_n = {key: 0 for key in KEYS}
    for iy in range(-n, n + 1):
        for ix in range(-n, n + 1):
            if math.hypot(ix * CELL_M, iy * CELL_M) > EXTENT_M:
                continue
            lat, lng = _offset(lat0, lng0, ix * CELL_M, iy * CELL_M)
            total += 1
            missing = [
                key
                for key in KEYS
                if not any(
                    haversine_m(lat, lng, plat, plng) <= RADIUS_M
                    for plat, plng in buckets[key]
                )
            ]
            for key in missing:
                missing_n[key] += 1
            if missing:
                cells.append(
                    {
                        "lat": lat,
                        "lng": lng,
                        "missing": missing,
                        "in_circle": point_in_polygon(lat, lng, polygon) if len(polygon) >= 3 else False,
                    }
                )

    ratio = {key: (missing_n[key] / total if total else 0.0) for key in KEYS}
    return {
        "grid_spacing_m": CELL_M,
        "walk_limit_m": RADIUS_M,
        "cell_count": total,
        "blind_count": len(cells),
        "blind_ratio": ratio,
        "cells": cells,
        "basis": "与地图相同：中心 1.5 公里、200 米方格，直线 1 公里",
    }
