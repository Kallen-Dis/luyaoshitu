"""分析时叠加附近的用户标注。

流程（见 main._analysis_steps）：

1. 查中心附近（网格范围 + 1 公里判定阈值，默认 2.5 公里）的有效标注，零配额；
2. 按策略挑出要用的：auto（默认）= 已核实的 + 自己的，外加用户逐条采纳的；
   all = 连他人待核实的也用；none = 纯算法。其余作为「建议」列出；
3. 围挡并进本次的围挡（去重、总数不超过 20 处）；设施失效从设施集合里剔除，
   补录设施加进去；人工灰色区域作为叠加层，不改变算法判定；
4. 先按纯算法算一遍（结果 A），再按叠加后的输入算一遍（结果 B）。B 用到的点对
   只要 A 测过就命中缓存，额外配额只花在补录设施与「下一家候选」上；
5. 页面展示 B，并附上 A 的分数与逐条标注的影响，写明用了哪几条标注的哪个版本。

附近没有要用的标注时（最常见）只算 A，和没有这个功能时完全一样。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from ..isochrone.geometry import first_entry_along, haversine_m, point_in_polygon
from ..isochrone.refine import MAX_CLOSURES, Closure
from ..poi.collect import CoverageResult, Poi, normalize_name
from .models import parse_time

MODES = ("auto", "all", "none")
MAX_FACILITY_MARKINGS = 10
CLOSURE_DEDUP_M = 20.0
FACILITY_MATCH_M = 50.0
FACILITY_NEAR_M = 15.0


def facility_matches(place: dict, spec: dict) -> bool:
    """同品类、规范名称和50米范围匹配，避免删除同址的不同门店。"""
    if place.get("category") != spec.get("category"):
        return False
    name = normalize_name(place.get("name", ""))
    target = normalize_name(spec.get("name", ""))
    return (
        bool(name and name == target)
        and haversine_m(place["lat"], place["lng"], spec["lat"], spec["lng"]) <= FACILITY_MATCH_M
    )


SUMMARY_KEYS = (
    "id",
    "version",
    "type",
    "title",
    "status",
    "mine",
    "source",
    "confidence",
    "disputed",
    "confirms",
    "disputes",
    "photo_count",
    "distance_m",
    "spec",
)


def _summary(marking: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {**{k: marking.get(k) for k in SUMMARY_KEYS}, **extra}


def _priority(m: dict[str, Any]) -> tuple:
    """自己的 > 已核实 > 置信度高 > 离中心近。"""
    return (
        not m.get("mine"),
        m.get("status") != "verified",
        -float(m.get("confidence") or 0),
        float(m.get("distance_m") or 0),
        int(m["id"]),
    )


def facility_priority(m: dict[str, Any]) -> tuple:
    """同类设施证据中，自己的最新记录优先；他人仍先看审核状态。"""
    timestamp = parse_time(m.get("updated_at") or m.get("created_at"))
    return (
        not m.get("mine"),
        False if m.get("mine") else m.get("status") != "verified",
        -(timestamp.timestamp() if timestamp else 0),
        -int(m["id"]),
    )


@dataclass
class MarkingPlan:
    mode: str
    nearby: list[dict[str, Any]]
    applied: list[dict[str, Any]] = field(default_factory=list)
    suggested: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    effects: dict[int, dict[str, Any]] = field(default_factory=dict)

    def of_type(self, *types: str) -> list[dict[str, Any]]:
        return [m for m in self.applied if m["type"] in types]

    def skip(self, marking: dict[str, Any], reason: str) -> None:
        self.applied = [m for m in self.applied if m["id"] != marking["id"]]
        self.skipped.append(_summary(marking, reason=reason))


def plan(
    nearby: list[dict[str, Any]],
    mode: str,
    include: list[int] | tuple[int, ...] = (),
    exclude: list[int] | tuple[int, ...] = (),
    *,
    walking: bool = True,
) -> MarkingPlan:
    """挑出这次要叠加的标注。nearby 已只含有效（待核实 / 已核实、未过期）的标注。"""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode}")
    wanted = set(include)
    unwanted = set(exclude)
    result = MarkingPlan(mode=mode, nearby=nearby)
    for m in sorted(nearby, key=_priority):
        if m["id"] in unwanted:
            result.skipped.append(_summary(m, reason="你选择了不使用这条"))
        elif mode == "none":
            result.suggested.append(_summary(m))
        elif m["status"] == "verified" or m.get("mine") or mode == "all" or m["id"] in wanted:
            result.applied.append(m)
        else:
            result.suggested.append(_summary(m))

    if not walking:
        for m in result.of_type("closure", "gray_area"):
            result.skip(m, "围挡与网格只在步行分析中叠加")
    facilities = sorted(result.of_type("facility_missing", "facility_extra"), key=facility_priority)
    for m in facilities[MAX_FACILITY_MARKINGS:]:
        result.skip(m, f"单次最多叠加 {MAX_FACILITY_MARKINGS} 条设施类标注")
    return result


def merge_closures(
    request: tuple[Closure, ...], marking_plan: MarkingPlan
) -> tuple[tuple[Closure, ...], dict[int, Closure]]:
    """把围挡类标注并进本次请求里的围挡。重合的只算一处，总数不超过 MAX_CLOSURES。"""
    merged = list(request)
    by_id: dict[int, Closure] = {}
    for m in sorted(marking_plan.of_type("closure"), key=_priority):
        spec = m["spec"]
        closure = Closure(
            float(spec["lat"]), float(spec["lng"]), float(spec["radius_m"]), f"标注#{m['id']}"
        )
        twin = next(
            (
                c
                for c in merged
                if haversine_m(c.lat, c.lng, closure.lat, closure.lng) <= CLOSURE_DEDUP_M
                and abs(c.radius_m - closure.radius_m) <= max(10.0, 0.3 * closure.radius_m)
            ),
            None,
        )
        if twin is not None:
            reason = (
                "与你这次画的临时围挡重合，按一处计算"
                if twin in request
                else "与另一条围挡标注重合，按一处计算"
            )
            marking_plan.skip(m, reason)
            continue
        if len(merged) >= MAX_CLOSURES:
            marking_plan.skip(m, f"超出单次 {MAX_CLOSURES} 处围挡上限")
            continue
        merged.append(closure)
        by_id[int(m["id"])] = closure
    return tuple(merged), by_id


def _similar(a: str, b: str) -> bool:
    na, nb = normalize_name(a), normalize_name(b)
    return bool(na and nb) and (na == nb or na in nb or nb in na)


def modify_coverage(coverage: CoverageResult, marking_plan: MarkingPlan) -> CoverageResult:
    """按设施类标注修正设施集合，返回新的 CoverageResult（不改动原对象）。"""
    results = [replace(r, pois=list(r.pois)) for r in coverage.results]
    by_cat = {r.category: r for r in results}
    handled = []
    for m in sorted(
        marking_plan.of_type("facility_missing", "facility_extra"), key=facility_priority
    ):
        spec = m["spec"]
        cat = spec["category"]
        if cat in coverage.failed or cat not in by_cat:
            marking_plan.skip(m, f"「{cat}」这次检索失败，无法按标注修正")
            continue
        if any(facility_matches(spec, previous) for previous in handled):
            marking_plan.skip(m, "已由优先级更高的设施证据处理")
            continue
        handled.append(spec)
        pois = by_cat[cat].pois
        lat, lng = float(spec["lat"]), float(spec["lng"])
        if m["type"] == "facility_missing":
            target = normalize_name(spec["name"])
            named = [
                (haversine_m(lat, lng, p.lat, p.lng), p)
                for p in pois
                if normalize_name(p.name) == target
            ]
            named = [x for x in named if x[0] <= FACILITY_MATCH_M]
            pick = min(named, key=lambda x: x[0], default=None)
            if pick is None:
                marking_plan.effects[int(m["id"])] = {"unmatched_exclusion": spec["name"]}
                continue  # 保留否定证据，后续出行补查也不得重新召回。
            pois.remove(pick[1])
            marking_plan.effects[int(m["id"])] = {"matched": pick[1].name}
        else:
            twin = next(
                (
                    p
                    for p in pois
                    if haversine_m(lat, lng, p.lat, p.lng) <= FACILITY_MATCH_M
                    and _similar(p.name, spec["name"])
                ),
                None,
            )
            if twin is not None and cat == "生鲜采买" and spec.get("sells_vegetables") is True:
                pois[pois.index(twin)] = replace(
                    twin,
                    fresh_status="verified",
                    fresh_evidence=f"用户现场确认销售蔬菜（标注 #{m['id']}，按采纳策略使用）",
                    marking_id=int(m["id"]),
                )
                marking_plan.effects[int(m["id"])] = {
                    "matched": twin.name,
                    "fresh_status": "verified",
                }
                continue
            if twin is not None:
                marking_plan.skip(m, f"地图已收录「{twin.name}」，不重复计算")
                continue
            pois.append(
                Poi(
                    name=spec["name"],
                    lat=lat,
                    lng=lng,
                    category=cat,
                    source="user",
                    marking_id=int(m["id"]),
                    fresh_status="verified"
                    if cat == "生鲜采买" and spec.get("sells_vegetables") is True
                    else None,
                    fresh_evidence=f"用户现场确认销售蔬菜（标注 #{m['id']}，按采纳策略使用）"
                    if spec.get("sells_vegetables")
                    else None,
                )
            )
    return CoverageResult(
        center=coverage.center,
        radius_m=coverage.radius_m,
        results=results,
        failed=list(coverage.failed),
        searches=coverage.searches,
    )


def route_hits(spec: dict[str, Any], rays: list[dict[str, Any]]) -> list[float]:
    """围挡挡住了哪几个方向的步行路线（用结果里存的路线基线，零配额）。"""
    circle = [(float(spec["lat"]), float(spec["lng"]), float(spec["radius_m"]))]
    hits = []
    for ray in rays:
        path = ray.get("route_path") or []
        if len(path) < 2:
            continue
        points = [(float(p[1]), float(p[0])) for p in path]
        if first_entry_along(points, circle) is not None:
            hits.append(float(ray.get("bearing") or 0.0))
    return hits


def _cells_by_key(blind: dict[str, Any] | None) -> dict[tuple[float, float], dict[str, Any]]:
    return {
        (round(float(c["lat"]), 5), round(float(c["lng"]), 5)): c
        for c in (blind or {}).get("cells") or []
    }


def _effect_text(m: dict[str, Any], data: dict[str, Any]) -> str:
    typ = m["type"]
    if typ == "closure":
        hits = data.get("rays", 0)
        text = (
            f"挡住 {hits} 个方向的步行路线" if hits else "不在任何一条步行路线上，对等时圈没有影响"
        )
        if data.get("places_inside"):
            text += f"；圈内 {data['places_inside']} 处设施视为暂不可用"
        return text
    if typ in ("facility_missing", "facility_extra"):
        cat = m["spec"]["category"]
        if "gained" in data:
            parts = []
            if data["cleared"]:
                parts.append(f"{data['cleared']} 格不再缺{cat}")
            if data["gained"]:
                parts.append(f"{data['gained']} 格变为缺{cat}")
            text = "；".join(parts) or "附近方格的判定没有变化"
        else:
            text = f"圈内{cat}由 {data.get('before', 0)} 处变为 {data.get('after', 0)} 处"
        if data.get("matched"):
            text = f"剔除了「{data['matched']}」，{text}"
        return text
    cells = data.get("cells", 0)
    agree = data.get("agree", 0)
    return f"人工标注，不改变算法判定；区域内 {cells} 个方格，其中 {agree} 格算法也判为缺失"


def compute_effects(
    marking_plan: MarkingPlan,
    *,
    rays: list[dict[str, Any]],
    blind_a: dict[str, Any] | None,
    blind_b: dict[str, Any] | None,
    coverage_a: dict[str, Any] | None,
    coverage_b: dict[str, Any] | None,
) -> None:
    """逐条算出标注对结果的影响，写进 marking_plan.effects。"""
    cells_a = _cells_by_key(blind_a)
    cells_b = _cells_by_key(blind_b)
    limit = float((blind_b or blind_a or {}).get("walk_limit_m") or 1000.0)
    places = (coverage_a or {}).get("places") or []
    for m in marking_plan.applied:
        mid = int(m["id"])
        spec = m["spec"]
        data = dict(marking_plan.effects.get(mid) or {})
        if m["type"] == "closure":
            data["rays"] = len(route_hits(spec, rays))
            data["places_inside"] = sum(
                1
                for p in places
                if haversine_m(float(p["lat"]), float(p["lng"]), spec["lat"], spec["lng"])
                <= spec["radius_m"]
            )
        elif m["type"] in ("facility_missing", "facility_extra"):
            cat = spec["category"]
            if cells_a and cells_b:
                gained = cleared = 0
                for key, cb in cells_b.items():
                    ca = cells_a.get(key)
                    if ca is None:
                        continue
                    if haversine_m(key[0], key[1], spec["lat"], spec["lng"]) > limit:
                        continue
                    was = cat in (ca.get("missing") or [])
                    now = cat in (cb.get("missing") or [])
                    gained += int(now and not was)
                    cleared += int(was and not now)
                data.update(gained=gained, cleared=cleared)
            else:
                data.update(
                    before=int(((coverage_a or {}).get("categories") or {}).get(cat, 0)),
                    after=int(((coverage_b or {}).get("categories") or {}).get(cat, 0)),
                )
        else:
            ring = [(float(p[1]), float(p[0])) for p in spec["polygon"]]
            inside = [c for key, c in cells_b.items() if point_in_polygon(key[0], key[1], ring)]
            data["cells"] = len(inside)
            data["agree"] = sum(
                1 for c in inside if set(c.get("missing") or []) & set(spec["categories"])
            )
        data["text"] = _effect_text(m, data)
        marking_plan.effects[mid] = data


CELL_FIELDS = ("missing", "unknown", "nearest_m", "reach_s", "reach_raw_s", "closure_blocked")


def changed_cells(
    blind_a: dict[str, Any] | None, blind_b: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """纯算法网格里与叠加后不同的格子（完整的 A 格）。前端拿它把 B 的网格还原成 A。

    两遍网格按同一中心、同一配置生成，格心坐标一致，按坐标对齐。
    """
    if not blind_a or not blind_b or blind_a is blind_b:
        return []
    cells_b = _cells_by_key(blind_b)
    out = []
    for key, ca in _cells_by_key(blind_a).items():
        cb = cells_b.get(key)
        if cb is None or any(ca.get(f) != cb.get(f) for f in CELL_FIELDS):
            out.append(ca)
    return out


def output(
    marking_plan: MarkingPlan,
    *,
    query_radius_m: float,
    report_a: dict[str, Any] | None,
    report_b: dict[str, Any] | None,
    props_a: dict[str, Any] | None,
    props_b: dict[str, Any],
    ring_a: list[list[float]] | None,
    blind_a: dict[str, Any] | None = None,
    blind_b: dict[str, Any] | None = None,
    coverage_a: dict[str, Any] | None = None,
    coverage_b: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """写进结果 properties.markings 的内容。

    baseline 除了纯算法的分数，还带上在地图上还原纯算法结果所需的差异：
    变了的格子、设施点（设施类标注改了集合时）、灰色区域、等时圈（围挡改了圈时）。
    前端据此在「纯算法 / 含标注」之间即时切换，不必重算。
    """
    applied = [
        _summary(m, effect=(marking_plan.effects.get(int(m["id"])) or {}).get("text", ""))
        for m in marking_plan.applied
    ]
    out: dict[str, Any] = {
        "mode": marking_plan.mode,
        "query_radius_m": query_radius_m,
        "nearby_count": len(marking_plan.nearby),
        "applied": applied,
        "suggested": marking_plan.suggested,
        "skipped": marking_plan.skipped,
        "baseline": None,
        "effect": None,
    }
    if report_a is not None and report_b is not None and props_a is not None:
        grays_a = (report_a.get("gray_regions") or {}).get("region_count")
        grays_b = (report_b.get("gray_regions") or {}).get("region_count")
        out["baseline"] = {
            "total": report_a.get("total"),
            "grade": report_a.get("grade"),
            "area_km2": props_a.get("area_km2"),
            "blind_cells": report_a.get("blind_cell_count"),
            "blind_in_circle": report_a.get("blind_in_circle"),
            "gray_regions": grays_a,
            # 只有围挡改变了等时圈时才带上纯算法的圈，前端画成对比虚线
            "ring": ring_a,
            "inner_rings": props_a.get("rings") if ring_a else None,
            "raw_ring": props_a.get("raw_ring") if ring_a else None,
            "cells_changed": changed_cells(blind_a, blind_b),
            "places": (coverage_a or {}).get("places")
            if coverage_a is not None and coverage_a is not coverage_b
            else None,
            "regions": report_a.get("gray_regions"),
        }

        def delta(a: Any, b: Any, digits: int = 1) -> float | None:
            if a is None or b is None:
                return None
            return round(float(b) - float(a), digits)

        out["effect"] = {
            "score_algorithm": report_a.get("total"),
            "score_with_markings": report_b.get("total"),
            "score_delta": delta(report_a.get("total"), report_b.get("total")),
            "blind_cells_delta": delta(
                report_a.get("blind_cell_count"), report_b.get("blind_cell_count"), 0
            ),
            "area_delta_km2": delta(props_a.get("area_km2"), props_b.get("area_km2"), 4),
            "gray_regions_delta": delta(grays_a, grays_b, 0),
        }
    return out
