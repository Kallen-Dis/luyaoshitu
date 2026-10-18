"""首次计算的路线复用与方向覆盖；所有数据仍经过同一位置质量检查。"""

import math

from .geometry import haversine_m
from .holepolicy import distance_to_ring
from .routequality import route_positions, usable_for_fusion


def reuse_ray_routes(chk, lattice):
    """射线阶段已经付费取得的响应直接复用，不靠不同精度缓存键再发请求。"""
    from .gridcheck import _add_hole_target

    if not getattr(chk, "origin_preflight", {}).get("passed") or chk.refine is None:
        return 0
    reused = 0
    for target, route in zip(chk.refine.targets, chk.refine.routes, strict=False):
        if not target or not route:
            continue
        positions = route_positions(route)
        if (
            not positions["complete"]
            or positions["position_basis"] != "step_and_path"
            or not positions["origin_consistent"]
            or not positions["origin"]
            or haversine_m(*positions["origin"], *chk.center) > 1
        ):
            continue
        p = _add_hole_target(chk, lattice, *target, "ray_route")
        if p.route_checked:
            continue
        chk.ingest_route(p, route)
        reused += int(usable_for_fusion(p, chk.center) and p.observed_location is not None)
    return reused


def spread_boundary_targets(chk, current, ordered):
    """每批最多一半位置优先补齐八方向边界，余量继续解决孔洞/测量冲突。"""
    rings = [
        [chk.xy(lat, lng) for lng, lat in face[0]]
        for face in current["geometry"]["coordinates"]
        if face and len(face[0]) >= 3
    ]

    def sector(lat, lng):
        x, y = chk.xy(lat, lng)
        if (
            math.hypot(x, y) < 100
            or min((distance_to_ring((x, y), ring) for ring in rings), default=math.inf) > 100
        ):
            return None
        return int((math.atan2(y, x) % math.tau) / (math.pi / 4))

    counts = [0] * 8
    seen = set()
    for p in chk.probes.values():
        if p.kind.startswith("validation") or not usable_for_fusion(p, chk.center):
            continue
        position = p.observed_location
        if position and position not in seen:
            seen.add(position)
            s = sector(*position)
            if s is not None:
                counts[s] += 1
    pools = {}
    for index, (item, predicted) in enumerate(ordered):
        lat, lng = (item.lat, item.lng) if hasattr(item, "lat") else item
        s = sector(lat, lng)
        if s is not None and counts[s] == 0:
            pools.setdefault(s, []).append(
                (abs((predicted or chk.cfg.target_s) - chk.cfg.target_s), index)
            )
    selected = [min(pool)[1] for _, pool in sorted(pools.items())][:4]
    return [ordered[i] for i in selected] + [
        item for i, item in enumerate(ordered) if i not in selected
    ], counts
