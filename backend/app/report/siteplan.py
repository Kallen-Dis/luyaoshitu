"""核验选址：把「补设」处方的估算交给真实路网复核，并在几个备选点位里挑实测最好的。

处方里的补设点是按直线距离 × 典型绕行估出来的。这里对某一品类：

1. 用同一套最大覆盖排序（prescribe.rank_sites）取前几名候选，彼此至少相隔 300 米，
   免得三个备选挤在相邻格子里、核验了等于没核验；
2. 每个候选跑一次「模拟新建」（simulate_facility）：缺这一类、且直线 1 公里内的方格到候选点
   做批量算路，步行 1 公里内的才算消去——圈内 100 米网格上每个候选约百个点对；
3. 按实测消去的格数重新排序，给最好的一处用逆地理编码写出「XX 路附近」。

点对预算默认 360，超出预算的候选不再核验并如实标注。离线模拟数据只做直线估算。
"""

from __future__ import annotations

from typing import Any

from ..baidu.client import BaiduMapClient
from ..baidu.errors import QuotaExhaustedError
from ..isochrone.geometry import haversine_m
from .prescribe import rank_sites, supply_demand, typical_detour
from .regions import classify_missing, region_labels
from .simulate import simulate_facility

ALTERNATIVE_GAP_M = 300.0

# 默认点对预算：够 3 个备选全部核验。桃浦（缺口最大的样例）三类前 3 个备选的上界分别是
# 317 / 279 / 347 个点对（缺这一类、且直线 1 公里内的方格数之和，命中缓存的不花配额）。
# 旧版 200 米网格时每个备选几十个点对，240 就够；网格加密到 100 米后 240 只够核验 2 个。
DEFAULT_BUDGET_PAIRS = 360


def site_candidates(
    feature: dict[str, Any], category: str, count: int = 3
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """该品类补设的前几名候选点（估算），以及估算口径。没有缺口格时返回空列表。"""
    props = feature.get("properties") or {}
    blindspots = props.get("blindspots") or {}
    cells = list(blindspots.get("cells") or [])
    if not cells:
        raise ValueError("当前结果没有网格盲区判定，无法选址。请先做一次含盲区判定的步行分析。")
    coverage = props.get("coverage") or {}
    if category in set(coverage.get("failed_categories") or []):
        raise ValueError(f"「{category}」检索失败，数量未知，不做选址。")
    limit = float(blindspots.get("walk_limit_m") or 1000.0)
    _, causes = classify_missing(cells, coverage, limit)
    demand = supply_demand(causes, category)
    detour = typical_detour(props)
    radius = limit / detour
    labels = region_labels(blindspots)

    picks: list[dict[str, Any]] = []
    for j, covered in rank_sites(cells, demand, radius):
        lat, lng = float(cells[j]["lat"]), float(cells[j]["lng"])
        if any(haversine_m(lat, lng, p["lat"], p["lng"]) < ALTERNATIVE_GAP_M for p in picks):
            continue
        ids = sorted({labels[i] for i in covered if labels.get(i)})
        picks.append(
            {
                "lat": lat,
                "lng": lng,
                "estimated": len(covered),
                "estimated_in_circle": sum(1 for i in covered if cells[i].get("in_circle", True)),
                "region": ids[0] if ids else None,
            }
        )
        if len(picks) >= count:
            break
    basis = {
        "demand_cells": len(demand),
        "detour": round(detour, 2),
        "radius_m": round(radius),
    }
    return picks, basis


async def plan_site(
    client: BaiduMapClient | None,
    feature: dict[str, Any],
    category: str,
    alternatives: int = 3,
    budget_pairs: int = DEFAULT_BUDGET_PAIRS,
) -> dict[str, Any]:
    """核验某一品类的补设选址。返回按实测效果排序的候选与最佳一处（含模拟结果）。"""
    picks, basis = site_candidates(feature, category, alternatives)
    if not picks:
        raise ValueError(
            f"「{category}」没有供给缺口方格（缺口都属于路网阻隔或根本不缺），不需要补设，"
            "应优先按「打通」处方处理。"
        )
    props = feature.get("properties") or {}
    cells = (props.get("blindspots") or {}).get("cells") or []
    limit = float((props.get("blindspots") or {}).get("walk_limit_m") or 1000.0)
    verify = client is not None and not props.get("simulated")

    used = 0
    quota_hit = False
    results: list[dict[str, Any]] = []
    for rank, p in enumerate(picks, start=1):
        # 模拟新建要测的点对 = 缺这一类、且到候选点直线 ≤ 1 公里的方格数
        need = sum(
            1
            for c in cells
            if category in (c.get("missing") or [])
            and haversine_m(float(c["lat"]), float(c["lng"]), p["lat"], p["lng"]) <= limit
        )
        entry: dict[str, Any] = {**p, "rank_estimate": rank, "pairs_needed": need}
        if verify and not quota_hit and used + need > budget_pairs:
            entry.update(status="over_budget", verified=None, simulation=None)
            results.append(entry)
            continue
        try:
            sim = await simulate_facility(
                client, feature, category, p["lat"], p["lng"], verify=verify and not quota_hit
            )
        except QuotaExhaustedError:
            quota_hit = True
            sim = await simulate_facility(None, feature, category, p["lat"], p["lng"], False)
        if sim["basis"] == "estimate_quota":
            quota_hit = True
        used += sim["pairs_used"]
        entry.update(
            status="verified" if sim["basis"] == "network" else "estimated",
            verified=sim["covered_count"] if sim["basis"] == "network" else None,
            simulation=sim,
        )
        results.append(entry)

    scored = [r for r in results if r["simulation"] is not None]
    best = max(
        scored,
        key=lambda r: (
            r["verified"] if r["verified"] is not None else -1,
            r["simulation"]["covered_count"],
            -r["rank_estimate"],
        ),
    )
    if verify and client is not None:
        best["place"] = await client.reverse_geocode(best["lat"], best["lng"])

    verified = [r for r in results if r["status"] == "verified"]
    if verified:
        note = (
            f"前 {len(results)} 个备选点（相隔至少 {ALTERNATIVE_GAP_M:.0f} 米）中，"
            f"{len(verified)} 个做了真实路网核验，共 {used} 个点对；"
            "实测消去的格数包含同一品类的路网阻隔格，所以可能多于估算的缺口格数。"
        )
    elif quota_hit:
        note = "批量算路配额已用尽，只按直线距离估算，结果是上限。"
    else:
        note = "离线模拟数据或未做核验：只按直线距离估算，结果是上限。"
    return {
        "category": category,
        "basis": basis,
        "candidates": [{k: v for k, v in r.items() if k != "simulation"} for r in results],
        "best": best,
        "pairs_used": used,
        "budget_pairs": budget_pairs,
        "verified": bool(verified),
        "note": note,
    }
