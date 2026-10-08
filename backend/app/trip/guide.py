"""沉浸引导的出行副本、当前位置补检索；不修改报告或写入历史。"""

import copy

from ..isochrone.geometry import first_entry_along, haversine_m, offset_point
from ..poi.catalog import by_name
from ..poi.collect import CoverageResult, collect_category
from .budget import TripBudget, Usage
from .candidates import TripError, place_id, point
from .context import matches
from .routing import TripRoutingClient


def check_origin(raw):
    lat, lng = point(raw)
    if not (3.8 <= lat <= 53.6 and 73.4 <= lng <= 135.1):
        raise TripError("invalid", "当前位置不在支持的地图范围内。")
    return lat, lng


def adopted_ids(feature):
    markings = feature["properties"].get("markings") or {}
    return sorted(
        set(markings.get("guide_adopted_ids", [])) | {m["id"] for m in markings.get("applied", [])}
    )


def fixed_scope(feature, origin, places):
    props = feature["properties"]
    coverage = props.setdefault("coverage", {})
    if "places" not in coverage:
        raise TripError("no_places", "原计划没有设施列表，请重新查询。")
    original_center = point(props.get("center") or {})
    span = haversine_m(*original_center, *check_origin(origin))
    if span > 19000:
        raise TripError("origin_out_of_range", "当前位置离原计划过远，请改选当前位置附近的设施。")
    for place in places:
        if by_name(str(place.get("category"))) is None:
            raise TripError("invalid", "所选设施类别无效。")
        point(place)
        if haversine_m(*original_center, *point(place)) > 19000:
            raise TripError("invalid", "所选设施超出本次重规划范围。")
    # 原响应补查出的设施也要进入 baseline；随后仍由最新共享失效标注筛除。
    sets = [coverage["places"]]
    baseline = ((props.get("markings") or {}).get("baseline") or {}).get("places")
    if baseline is not None:
        sets.append(baseline)
    for collection in sets:
        known = {place_id(p) for p in collection}
        for place in places:
            if place_id(place) not in known:
                collection.append(copy.deepcopy(place))
                known.add(place_id(place))
    farthest = max([span, *[haversine_m(*original_center, *point(p)) for p in places]])
    coverage["radius_m"] = min(20000, max(float(coverage.get("radius_m") or 2500), farthest + 500))


async def nearby_feature(base, settings, origin, category, original):
    center = check_origin(origin)
    usage = Usage()
    budget = TripBudget(settings.markings_dir / "trip-budget.sqlite3", settings)
    client = TripRoutingClient(base, budget, usage, memory_only=True)
    result = await collect_category(client, by_name(category), center, 2500)
    if result is None:
        raise TripError(
            "search_failed", "当前位置设施检索失败或配额不足，原计划保留；不能据此认为没有设施。"
        )
    ring = [offset_point(*center, bearing, 2500) for bearing in range(0, 360, 10)]
    feature = {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[lng, lat] for lat, lng in [*ring, ring[0]]]],
        },
        "properties": {
            "center": {"lat": center[0], "lng": center[1]},
            "minutes": 15,
            "simulated": False,
            "coverage": CoverageResult(center, 2500, results=[result]).as_dict(),
            "markings": {
                "guide_adopted_ids": adopted_ids(original),
                "applied": copy.deepcopy(
                    (original["properties"].get("markings") or {}).get("applied", [])
                ),
            },
        },
    }
    return feature, usage


def routing_feature(feature):
    """只返回下一次出行需要的数据；保存在前端内存，不替换体检结果。"""
    props = feature["properties"]
    return {
        "type": "Feature",
        "geometry": copy.deepcopy(feature["geometry"]),
        "properties": {
            k: copy.deepcopy(props[k])
            for k in ("center", "coverage", "markings", "minutes", "simulated")
            if k in props
        },
    }


def route_scope(result):
    paths = [[(lat, lng) for lng, lat in leg["route"]["path"]] for leg in result["legs"]]
    points = [p for path in paths for p in path]
    points.extend(point(leg["place"]) for leg in result["legs"] if leg.get("place"))
    points.extend(point(leg["from"]) for leg in result["legs"])
    center = (
        (min(p[0] for p in points) + max(p[0] for p in points)) / 2,
        (min(p[1] for p in points) + max(p[1] for p in points)) / 2,
    )
    radius = max(haversine_m(*center, *p) for p in points) + 300
    if radius > 20000:
        raise TripError("route_scope_limit", "绕行范围过大，无法完成沿途核验，请改选附近设施。")
    return center, radius, paths


def relevant_evidence(markings, result, paths):
    places = [leg.get("place") or leg for leg in result["legs"]]
    selected = []
    for marking in markings:
        spec = marking.get("spec") or {}
        if marking["type"] in ("facility_extra", "facility_missing"):
            if any(matches(place, spec) for place in places):
                selected.append(marking)
        elif marking["type"] == "closure":
            circle = (spec["lat"], spec["lng"], spec["radius_m"])
            if any(first_entry_along(path, [circle]) is not None for path in paths):
                selected.append(marking)
    return selected
