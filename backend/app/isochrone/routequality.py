"""路线位置来源核对：响应坐标不假定为道路吸附点，折线也不假定完整。"""

import math

from .geometry import haversine_m


def location(value):
    if not isinstance(value, dict):
        return None
    try:
        p = float(value["lat"]), float(value["lng"])
        return (
            p if all(math.isfinite(v) for v in p) and abs(p[0]) <= 90 and abs(p[1]) <= 180 else None
        )
    except (KeyError, TypeError, ValueError):
        return None


def route_positions(route):
    steps = route.get("steps") or []
    parts = [s.get("path") or [] for s in steps]
    parts = [p for p in parts if p]
    points = [p for part in parts for p in part]
    try:
        if any(len(p) != 2 or location({"lat": p[0], "lng": p[1]}) is None for p in points):
            points = []
    except TypeError:
        points = []
    start = tuple(points[0]) if points else None
    end = tuple(points[-1]) if points else None
    step_start = location(steps[0].get("start_location")) if steps else None
    step_end = location(steps[-1].get("end_location")) if steps else None
    endpoint_agreement = bool(points) and all(
        other is None or haversine_m(*p, *other) <= 2
        for p, other in ((start, step_start), (end, step_end))
    )
    for step in steps:
        path = step.get("path") or []
        for field, index in (("start_location", 0), ("end_location", -1)):
            if step.get(field) is not None:
                declared = location(step[field])
                if (
                    not points
                    or not path
                    or declared is None
                    or haversine_m(*path[index], *declared) > 2
                ):
                    endpoint_agreement = False
    continuous = (
        bool(points)
        and all(haversine_m(*a[-1], *b[0]) <= 2 for a, b in zip(parts, parts[1:], strict=False))
        and all(s.get("path") for s in steps)
    )
    # 顶层 origin 是路线请求起点，step.start_location 是分段折线起点。
    # 二者不能混为一谈；只接受明确记录且相互吻合的请求/响应位置。
    requested = route.get("requested_origin")
    try:
        requested = location({"lat": requested[0], "lng": requested[1]}) if requested else None
    except (TypeError, IndexError, KeyError):
        requested = None
    response = location(route.get("response_origin"))
    declared = route.get("requested_origin") is not None or route.get("response_origin") is not None
    origin = requested if declared else start
    origin_consistent = (
        bool(requested and response and haversine_m(*requested, *response) <= 1)
        if declared
        else bool(start)
    )
    gap = haversine_m(*origin, *start) if origin and start else None
    return {
        "origin": origin,
        "origin_consistent": origin_consistent,
        "origin_basis": "request_and_response" if declared else "path_start",
        "start_gap_m": gap,
        "start": start,
        "end": end,
        "path": [tuple(p) for p in points],
        "complete": bool(continuous and endpoint_agreement),
        "position_basis": "step_and_path" if step_start and step_end else "path_only",
        "endpoint_agreement": endpoint_agreement,
        "response_origin": route.get("response_origin"),
        "response_destination": route.get("response_destination"),
    }


def same_origin(point, center):
    """固定请求起点；分段折线开头不是整个请求的出发位置，不虚构中间连线。"""
    if getattr(point, "route_origin_basis", "path_start") == "request_and_response":
        origin = point.route_origin
        return bool(point.route_origin_consistent and origin and haversine_m(*origin, *center) <= 1)
    return point.route_start is not None and haversine_m(*point.route_start, *center) <= 1


def path_prefix_usable(point, center):
    """2米只处理坐标舍入临界带；缺失起步段不补线，终点测时单独使用。"""
    return bool(
        same_origin(point, center)
        and point.route_start is not None
        and haversine_m(*point.route_start, *center) <= 52
    )


def effective_origin(point):
    return (
        point.route_origin
        if getattr(point, "route_origin_basis", "path_start") == "request_and_response"
        else point.route_start
    )


def usable_for_fusion(point, center):
    return (
        same_origin(point, center)
        and point.route_position_basis == "step_and_path"
        and point.route_position_consistent
        and point.route_geometry_complete
        and not point.blocked
    )


async def check_origin(chk, points):
    """开发补测前最多取四方向各一条路线，不移动输入中心，不突破原预算。"""
    groups = {}
    for p in points:
        x, y = chk.xy(p.lat, p.lng)
        if math.hypot(x, y) < 50:
            continue
        sector = int((math.atan2(y, x) % math.tau) / (math.pi / 2))
        groups.setdefault(sector, []).append(p)
    # 所有网络重试也由预算包装器计数，预检最多新增八次请求。
    old_limit = getattr(chk.client, "route_limit", None)
    if old_limit is not None:
        chk.client.route_limit = min(old_limit, chk.client.route_attempts + 8)
    records = []
    for sector, candidates in sorted(groups.items()):
        p = min(candidates, key=lambda p: abs(math.hypot(*chk.xy(p.lat, p.lng)) - 200))
        try:
            await chk.verify_by_route(p)
        except Exception:
            if old_limit is not None:
                chk.client.route_limit = old_limit
            raise
        ok = (
            usable_for_fusion(p, chk.center)
            and p.route_s is not None
            and p.observed_location is not None
        )
        records.append(
            {
                "sector": sector,
                "usable": ok,
                "origin_error_m": haversine_m(*effective_origin(p), *chk.center)
                if effective_origin(p)
                else None,
                "position_basis": p.route_position_basis,
                "requested": [p.lng, p.lat],
                "actual_origin": list(reversed(effective_origin(p)))
                if effective_origin(p)
                else None,
                "path_start": list(reversed(p.route_start)) if p.route_start else None,
                "path_start_gap_m": haversine_m(*p.route_start, *chk.center)
                if p.route_start
                else None,
                "origin_basis": p.route_origin_basis,
                "origin_consistent": p.route_origin_consistent,
            }
        )
    if old_limit is not None:
        chk.client.route_limit = old_limit
    passed = sum(r["usable"] for r in records) >= 3
    starts = [tuple(reversed(r["actual_origin"])) for r in records if r["actual_origin"]]
    groups = []
    for start in starts:
        group = next((g for g in groups if all(haversine_m(*start, *p) <= 1 for p in g)), None)
        if group is None:
            groups.append([start])
        else:
            group.append(start)
    errors = [r["origin_error_m"] for r in records if r["origin_error_m"] is not None]
    reason = (
        "起点来源核对通过；首段缺口只影响路径约束"
        if passed
        else "路线请求与顶层响应的起点记录不一致或不完整"
        if any(
            r["origin_basis"] == "request_and_response" and not r["origin_consistent"]
            for r in records
        )
        else "部分路线起步段信息不足，可用终点证据不足三个方向"
        if any((r["path_start_gap_m"] or 0) > 50 for r in records)
        else "多个方向返回了不同道路起点，请先选择稳定的步行入口"
        if len(groups) > 1
        else "返回道路起点偏离输入中心，请确认步行入口后统一重算"
        if errors and max(errors) > 1
        else "完整位置证据或可用预算不足"
    )
    return {
        "passed": passed,
        "checked_directions": len(records),
        "records": records,
        "origin_groups": len(groups),
        "reason": reason,
        "suggested_origin": list(reversed(starts[0]))
        if not passed
        and len(starts) == len(records)
        and len(groups) == 1
        and all(r["origin_basis"] == "path_start" for r in records)
        else None,
        "policy": "固定请求起点，核对请求及顶层响应；"
        "分段起点单独记录，缺失连接段不补线；失败只读缓存",
    }
