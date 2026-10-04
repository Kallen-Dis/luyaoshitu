"""模拟新建：「假如在这里新建一处设施」，哪些盲区网格会被消去。

口径与盲区判定一致——**步行 1 公里**，不是直线 1 公里：

1. 直线距离是步行距离的下界，只有缺这一类、且到拟建点直线 ≤ 1 公里的网格才可能被覆盖；
2. 这些候选网格到拟建点做一次批量算路（多起点 × 1 终点，通常不超过 100 个点对），
   步行距离达标的才算真正消去。

离线模拟数据、或调用方关闭核验时，只做第 1 步，结果是**上限估算**并如实标注。
拟建点不做施工围挡核验；真实效果需设施建成后按路网复测。
"""

from __future__ import annotations

import copy
from typing import Any

from ..baidu.client import BaiduMapClient
from ..baidu.errors import QuotaExhaustedError
from ..isochrone.geometry import haversine_m
from .score import build_report


def _recount(blindspots: dict[str, Any], cells: list[dict[str, Any]]) -> dict[str, Any]:
    """按新的网格判定重算盲区计数与各品类占比（测距失败的网格不进分母）。"""
    out = dict(blindspots)
    out["cells"] = cells
    out["blind_count"] = sum(1 for c in cells if c.get("missing"))
    names = set(blindspots.get("blind_ratio") or {})
    for c in cells:
        names.update(c.get("missing") or [])
    # 判定不足、本来就没给占比的品类保持不给，否则模拟前后的评分口径不一致
    names -= set(blindspots.get("incomplete_categories") or [])
    ratio: dict[str, float] = {}
    for name in sorted(names):
        judged = [c for c in cells if name not in (c.get("unknown") or [])]
        if judged:
            ratio[name] = round(
                sum(1 for c in judged if name in (c.get("missing") or [])) / len(judged), 3
            )
    out["blind_ratio"] = ratio
    return out


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
    blindspots = props.get("blindspots") or {}
    cells = list(blindspots.get("cells") or [])
    if not cells:
        raise ValueError("当前结果没有网格盲区判定，无法模拟新建。请先做一次含盲区判定的步行分析。")
    limit = float(blindspots.get("walk_limit_m") or 1000.0)
    coverage = props.get("coverage")

    candidates = [
        i
        for i, c in enumerate(cells)
        if category in (c.get("missing") or [])
        and haversine_m(float(c["lat"]), float(c["lng"]), lat, lng) <= limit
    ]

    covered: list[int] = candidates
    pairs_before = client.matrix_pairs if client is not None else 0
    basis = "estimate"
    unverified = 0
    if candidates and verify and client is not None and not props.get("simulated"):
        try:
            table = await client.walking_matrix_grid(
                [(float(cells[i]["lat"]), float(cells[i]["lng"])) for i in candidates],
                [(lat, lng)],
            )
        except QuotaExhaustedError:
            basis = "estimate_quota"
        else:
            basis = "network"
            covered = []
            for i, row in zip(candidates, table, strict=True):
                entry = row[0]
                if entry is None:
                    unverified += 1
                elif entry["distance_m"] <= limit:
                    covered.append(i)
    pairs_used = (client.matrix_pairs - pairs_before) if client is not None else 0

    after_cells = copy.deepcopy(cells)
    for i in covered:
        after_cells[i]["missing"] = [m for m in after_cells[i]["missing"] if m != category]
    after_blind = _recount(blindspots, after_cells)

    before_report = build_report(props, coverage, blindspots)
    after_report = build_report(props, coverage, after_blind)

    if basis == "network":
        approximation = (
            f"候选方格到拟建点做了真实路网测距（本次 {pairs_used} 个点对），步行 "
            f"{limit / 1000:.0f} 公里内的才算消去"
            + (f"；{unverified} 格测距失败，未计入" if unverified else "")
            + "。拟建点未做施工围挡核验，建成后需按路网复测。"
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

    return {
        "category": category,
        "lat": lat,
        "lng": lng,
        "basis": basis,
        "covered_cells": [
            {"lat": float(cells[i]["lat"]), "lng": float(cells[i]["lng"])} for i in covered
        ],
        "covered_count": len(covered),
        "candidate_count": len(candidates),
        "pairs_used": pairs_used,
        "before": _stats(before_report),
        "after": _stats(after_report),
        "approximation": approximation,
    }
