"""模拟新建：「假如在这里新建一处设施」，哪些盲区网格会被消去。

口径与盲区判定一致——**步行 1 公里**，不是直线 1 公里：

1. 使用带30米经验误差余量的直线候选筛选，缺这一类且直线 ≤ 1030米的格子才测距；
2. 这些候选网格到拟建点做一次批量算路（多起点 × 1 终点，通常不超过 100 个点对），
   步行距离达标的才算真正消去。

离线模拟数据、或调用方关闭核验时，只做第 1 步，结果是**上限估算**并如实标注。
存在已知围挡时按预算核验实际路线；真实效果需设施建成后按路网复测。
"""

from __future__ import annotations

import copy
from typing import Any

from ..baidu.client import BaiduMapClient
from ..baidu.errors import QuotaExhaustedError
from ..isochrone.geometry import first_entry_along, haversine_m, point_in_polygon
from ..trip.context import closures_from_feature
from .blindspot import REACH_GRID_M
from .score import build_report
from .subgrid import display_key, origin_groups, recount, refined_view, total

_recount = recount  # 旧名字，测试与调用方还在用


def _count(v: float) -> float | int:
    v = round(v * 4) / 4
    return int(v) if v == int(v) else v


def _stats(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "score": report.get("total"),
        "grade": report.get("grade"),
        "blind_count": report.get("blind_cell_count"),
        "blind_ratio": report.get("blind_ratio"),
    }


async def simulate_facility(
    client: BaiduMapClient | None,
    feature: dict[str, Any],
    category: str,
    lat: float,
    lng: float,
    verify: bool = True,
) -> dict[str, Any]:
    """返回模拟新建前后的对比。没有网格判定结果时抛 ValueError。"""
    props = dict(feature.get("properties") or {})
    raw_blind = props.get("blindspots") or {}
    # 做过 50 米细分时在派生的 50 米格网上模拟：实测过这一类的子格从子格中心出发测距，
    # 其余子格继承父格，从父格心出发（同一父格只测一次）。计数按 100 米格折算
    blindspots = refined_view(raw_blind) or {}
    cells = list(blindspots.get("cells") or [])
    if not cells:
        raise ValueError("当前结果没有网格盲区判定，无法模拟新建。请先做一次含盲区判定的步行分析。")
    limit = float(blindspots.get("walk_limit_m") or 1000.0)
    coverage = props.get("coverage")
    grid_evaluated = category in (blindspots.get("blind_ratio") or {}) or any(
        category in (cell.get("missing") or [])
        or (cell.get("nearest_m") or {}).get(category) is not None
        for cell in cells
    )

    candidates = [
        i
        for i, c in enumerate(cells)
        if category in (c.get("missing") or [])
        and haversine_m(float(c["lat"]), float(c["lng"]), lat, lng) <= limit + 30
    ]

    covered: list[int] = candidates
    pairs_before = client.matrix_pairs if client is not None else 0
    basis = "estimate"
    unverified = 0
    route_checks = 0
    circles = [(c["lat"], c["lng"], c["radius_m"]) for c in closures_from_feature(feature)]
    inside_closure = any(haversine_m(lat, lng, a, b) <= r for a, b, r in circles)
    if candidates and verify and client is not None and not props.get("simulated"):
        parents, quarters = origin_groups(cells, candidates, category)
        measured: dict[int, tuple[tuple[float, float], dict[str, float] | None]] = {}
        try:
            for groups, fine in ((parents, None), (quarters, REACH_GRID_M)):
                if not groups:
                    continue
                origins = list(groups)
                kwargs = {"origin_grid_m": fine} if fine else {}
                table = await client.walking_matrix_grid(origins, [(lat, lng)], **kwargs)
                for origin, row in zip(origins, table, strict=True):
                    for i in groups[origin]:
                        measured[i] = (origin, row[0])
        except QuotaExhaustedError:
            basis = "estimate_quota"
        else:
            basis = "network"
            covered = []
            unverified_cells: list[int] = []
            route_seen: dict[tuple[float, float], bool | None] = {}
            for i in candidates:
                origin, entry = measured.get(i, (None, None))
                if entry is None or origin is None:
                    unverified_cells.append(i)
                elif entry["distance_m"] <= limit:
                    if inside_closure:
                        continue
                    if circles:
                        if origin not in route_seen:
                            cap = getattr(getattr(client, "_s", None), "trip_request_routes", 12)
                            if route_checks >= cap:
                                route_seen[origin] = None
                            else:
                                route_checks += 1
                                raw = await client.walking_route(origin, (lat, lng))
                                points = [
                                    tuple(p)
                                    for s in (raw or {}).get("steps", [])
                                    for p in s.get("path", [])
                                ]
                                route_seen[origin] = (
                                    None
                                    if len(points) < 2
                                    else first_entry_along(points, circles) is None
                                )
                        clear = route_seen[origin]
                        if clear is None:
                            unverified_cells.append(i)
                            continue
                        if not clear:
                            continue
                    covered.append(i)
            unverified = total(cells, unverified_cells)
    pairs_used = (client.matrix_pairs - pairs_before) if client is not None else 0
    if inside_closure:
        covered = []

    after_cells = copy.deepcopy(cells)
    for i in covered:
        after_cells[i]["missing"] = [m for m in after_cells[i]["missing"] if m != category]
    after_blind = recount(blindspots, after_cells)

    before_report = build_report(props, coverage, raw_blind)
    after_coverage = copy.deepcopy(coverage)
    polygon = [(y, x) for x, y in feature["geometry"]["coordinates"][0]]
    inside = point_in_polygon(lat, lng, polygon)
    if after_coverage is not None and not inside_closure:
        after_coverage.setdefault("places", []).append(
            {
                "name": f"拟建{category}",
                "category": category,
                "lat": lat,
                "lng": lng,
                "in_circle": inside,
            }
        )
        if inside:
            after_coverage.setdefault("categories", {})[category] = (
                after_coverage.get("categories", {}).get(category, 0) + 1
            )
        after_coverage.setdefault("nearby_categories", {})[category] = (
            after_coverage.get("nearby_categories", {}).get(category, 0) + 1
        )
    after_report = build_report(props, after_coverage, after_blind)

    if not grid_evaluated:
        approximation = "本品类尚无逐格步行测距，本次比较圈内设施数量和评分；灰色区域保持原判定。"
    elif not candidates:
        approximation = "拟建点周围没有可改善的已判定缺失网格，本次只比较设施覆盖与评分。"
    elif basis == "network":
        approximation = (
            f"候选方格到拟建点做了真实路网测距（本次 {pairs_used} 个点对），步行 "
            f"{limit / 1000:.0f} 公里内的才算消去"
            + (f"；{_count(unverified)} 格测距失败，未计入" if unverified else "")
            + (f"；已按已知围挡核验 {route_checks} 条路线" if circles else "")
            + "。按现有路网评估，建成后需复测。"
        )
    elif basis == "estimate_quota":
        approximation = (
            "批量算路配额已用尽，只按直线距离估算：直线 1 公里内的方格全部视为消去，"
            "这是上限，真实步行覆盖只会更少。"
        )
    else:
        approximation = (
            "未做路网核验，只按直线距离估算：直线 1 公里内的方格全部视为消去，"
            "这是上限，真实步行覆盖只会更少。"
        )

    def facility_count(stats: dict[str, Any] | None) -> int | None:
        if not stats or category in (stats.get("failed_categories") or []):
            return None
        return (stats.get("categories") or {}).get(category)

    return {
        "category": category,
        "lat": lat,
        "lng": lng,
        "basis": basis,
        # 按地图上画的方格给坐标：细分过的格是子格中心，没细分的是父格心（同一格只列一次）
        "covered_cells": [
            {"lat": la, "lng": ln}
            for la, ln in dict.fromkeys(display_key(cells[i]) for i in covered)
        ],
        "covered_count": _count(total(cells, covered)),
        "candidate_count": _count(total(cells, candidates)),
        "pairs_used": pairs_used,
        "route_checks": route_checks,
        "grid_evaluated": grid_evaluated,
        "in_circle": inside,
        "inside_closure": inside_closure,
        "facility_count_before": facility_count(coverage),
        "facility_count_after": facility_count(after_coverage),
        "before": _stats(before_report),
        "after": _stats(after_report),
        "approximation": approximation,
    }
