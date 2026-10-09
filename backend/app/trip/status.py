"""行程可用性与排序最优性分开；每个响应分支使用相同的通行口径。"""


def plan_status(origin: dict, stops: list[str], legs: list[dict], basis: str) -> str:
    if any(leg.get("closure_status") == "blocked" for leg in legs):
        return "blocked"
    if basis != "network" or not legs or len(legs) != len(stops):
        return "unverified"
    previous = origin
    for category, leg in zip(stops, legs, strict=True):
        route = leg.get("route") or {}
        start = leg.get("from") or {}
        if (
            leg.get("category") != category
            or leg.get("closure_status") != "clear"
            or len(route.get("path") or []) < 2
            or not route.get("steps")
            or any(abs(start.get(k, 0) - previous.get(k, 0)) > 1e-6 for k in ("lat", "lng"))
        ):
            return "unverified"
        previous = leg
    return "clear"
