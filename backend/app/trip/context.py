"""将有效标注叠加到出行设施副本；不修改体检评分或历史。"""

from __future__ import annotations

import copy

from ..isochrone.geometry import haversine_m, point_in_polygon
from ..markings import apply
from ..poi.fresh import annotate_coverage, fields

matches = apply.facility_matches


def overlay(feature: dict, marking_plan: apply.MarkingPlan) -> None:
    props = feature["properties"]
    coverage = props.get("coverage") or {}
    old = props.get("markings") or {}
    baseline = (old.get("baseline") or {}).get("places")
    places = copy.deepcopy(baseline if baseline is not None else coverage.get("places", []))
    # 无还原差异的旧结果不能继续保留已撤回的补录点。
    if baseline is None:
        places = [p for p in places if not (p.get("source") == "user" and p.get("marking_id"))]
        active_ids = {m["id"] for m in marking_plan.applied}
        for place in places:
            if place.get("marking_id") and place["marking_id"] not in active_ids:
                place.pop("fresh_status", None)
                place.pop("fresh_evidence", None)
                place.pop("marking_id", None)
    polygon = [(lat, lng) for lng, lat in feature["geometry"]["coordinates"][0]]
    handled: list[dict] = []
    for marking in sorted(marking_plan.applied, key=apply.facility_priority):
        if marking["type"] not in ("facility_extra", "facility_missing"):
            continue
        spec = marking["spec"]
        if any(matches(spec, previous) for previous in handled):
            marking_plan.skip(marking, "已由优先级更高的设施证据处理")
            continue  # plan 已按信任优先级排序，最强证据先决定。
        handled.append(spec)
        if marking["type"] == "facility_missing":
            places = [p for p in places if not matches(p, spec)]
            continue
        twin = next((p for p in places if matches(p, spec)), None)
        if twin is None:
            twin = {k: spec[k] for k in ("name", "category", "lat", "lng")}
            twin.update(source="user", marking_id=marking["id"])
            twin["in_circle"] = point_in_polygon(spec["lat"], spec["lng"], polygon)
            places.append(twin)
        if spec.get("sells_vegetables") is True:
            twin.update(
                fresh_status="verified", fresh_evidence="用户现场确认销售蔬菜，按采纳策略使用"
            )
        twin.update(fields(twin))
    coverage["places"] = places
    props["coverage"] = coverage
    props["closures"] = []  # 临时围挡已移到规划模拟，不进入正式 HTTP 出行。
    apply.merge_closures((), marking_plan)  # 共享围挡沿用体检的去重和数量上限。
    props["markings"] = {
        **old,
        "applied": marking_plan.applied,
        "suggested": marking_plan.suggested,
    }
    annotate_coverage(coverage)


def closures_from_feature(feature: dict) -> list[dict]:
    props = feature["properties"]
    closures = list(props.get("closures") or [])
    for marking in (props.get("markings") or {}).get("applied", []):
        if marking.get("type") != "closure":
            continue
        spec = marking.get("spec") or {}
        if not all(k in spec for k in ("lat", "lng", "radius_m")):
            continue
        if not any(
            haversine_m(c["lat"], c["lng"], spec["lat"], spec["lng"]) <= 15
            and abs(c["radius_m"] - spec["radius_m"]) <= 10
            for c in closures
        ):
            closures.append({k: spec[k] for k in ("lat", "lng", "radius_m")})
    return closures
