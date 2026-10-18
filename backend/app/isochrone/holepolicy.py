"""孔洞和绕行证据的关联。线索不是墙，未复测的跳变不能切开耗时场。"""

import math

from .tin import inside_ring, ring_area


def distance_to_ring(point, ring):
    distances = []
    for a, b in zip(ring, (*ring[1:], ring[0]), strict=True):
        dx, dy = b[0] - a[0], b[1] - a[1]
        f = max(
            0,
            min(
                1, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / max(dx * dx + dy * dy, 1e-12)
            ),
        )
        distances.append(math.dist(point, (a[0] + f * dx, a[1] + f * dy)))
    return min(distances, default=math.inf)


def associated_clues(ring, clues):
    """只关联较慢端在孔内、较快端在孔外且邻近的原始点对。"""
    return [
        c
        for c in clues
        if inside_ring(*c["far"], ring)
        and not inside_ring(*c["near"], ring)
        and distance_to_ring(c["near"], ring) <= 150
    ]


def shape_stable(ring, alternatives):
    """几何稳定性工程门槛：面积变化不超过25%，双向边界偏移不超过25米。"""
    area = abs(ring_area(ring))
    for other in alternatives or []:
        if ring_area(other) >= 0 or abs(abs(ring_area(other)) - area) > area * 0.25:
            continue

        def samples(poly):
            for a, b in zip(poly, (*poly[1:], poly[0]), strict=True):
                count = max(1, math.ceil(math.dist(a, b) / 10))
                for i in range(count):
                    yield (a[0] + (b[0] - a[0]) * i / count, a[1] + (b[1] - a[1]) * i / count)

        if (
            max((distance_to_ring(p, other) for p in samples(ring)), default=math.inf) <= 25
            and max((distance_to_ring(p, ring) for p in samples(other)), default=math.inf) <= 25
        ):
            return True
    return False


def topology_stable(rings, alternatives):
    """孔洞须一一对应；新增、消失、分裂或合并都不能只检查原来的洞。"""
    holes = [r for r in rings if ring_area(r) < 0]
    others = [r for r in alternatives or [] if ring_area(r) < 0]
    if alternatives is None or len(holes) != len(others):
        return False
    matches = [
        [
            j
            for j, other in enumerate(others)
            if shape_stable(r, [other]) and shape_stable(other, [r])
        ]
        for r in holes
    ]
    owners = {}

    def assign(i, visited):
        for j in matches[i]:
            if j not in visited:
                visited.add(j)
                if j not in owners or assign(owners[j], visited):
                    owners[j] = i
                    return True
        return False

    return all(assign(i, set()) for i in range(len(holes)))


def smoothing_factor(a, b, clues):
    """原始相邻点对定位局部影响；保留耦合，不用简化显示折线当硬障碍。"""
    for c in clues:
        if c["status"] != "confirmed":
            continue
        near, far = c["near"], c["far"]
        gap = math.dist(near, far)
        if gap <= 1:
            continue
        mid = ((near[0] + far[0]) / 2, (near[1] + far[1]) / 2)
        normal = (far[0] - near[0], far[1] - near[1])
        sa = sum((a[i] - mid[i]) * normal[i] for i in (0, 1))
        sb = sum((b[i] - mid[i]) * normal[i] for i in (0, 1))
        if sa * sb < 0 and max(math.dist(a, mid), math.dist(b, mid)) <= gap * 0.75:
            return 0.25
    return 1.0


def computation_clues(chk):
    """显示线段仅最强点对经过直接复测；绝不把整段升级成已测障碍。"""
    out = []
    for c in getattr(chk, "fusion_clues", []):
        v = c.get("verification") or {}
        if not v.get("from") or not v.get("to"):
            continue
        near, far = (chk.xy(p[1], p[0]) for p in (v["from"], v["to"]))
        # 必须同时有同起点完整路线支持两侧，矩阵线索只用于诊断。
        from .routequality import usable_for_fusion

        matched = []
        for pos in (near, far):
            found = [
                p
                for p in chk.probes.values()
                if usable_for_fusion(p, chk.center)
                and p.route_s is not None
                and p.route_endpoint
                and math.dist(chk.xy(*p.route_endpoint), pos) <= 2
            ]
            matched.append(min(found, key=lambda p: p.route_s) if found else None)
        a, b = matched
        reliable = bool(
            a
            and b
            and a.route_s is not None
            and b.route_s is not None
            and b.route_s - a.route_s > 120
            and a.route_m is not None
            and b.route_m is not None
            and b.route_m - a.route_m > 2 * math.dist(near, far) + 60
        )
        out.append(
            {
                "id": c["id"],
                "near": near,
                "far": far,
                "status": "confirmed"
                if c["status"] == "confirmed" and reliable and v.get("route_positions_verified")
                else "unverified",
                "basis": "同起点路线跳变及原始点对复测" if reliable else "仅选点线索",
            }
        )
    return out


LOCAL_HOLE_AREA_M2 = 400.0
LOCAL_HOLE_SPAN_M = 40.0


def is_local_hole(ring):
    return (
        bool(ring)
        and abs(ring_area(ring)) <= LOCAL_HOLE_AREA_M2
        and max(math.dist(a, b) for a in ring for b in ring) <= LOCAL_HOLE_SPAN_M
    )


def localized_topology_stable(rings, alternatives, area_limit):
    """只容许有界微小孔洞的变化；大孔洞仍逐一匹配，加密失败不算通过。"""
    if alternatives is None:
        return False
    for group in (rings, alternatives):
        small = [r for r in group if ring_area(r) < 0 and is_local_hole(r)]
        if len(small) > 16 or sum(abs(ring_area(r)) for r in small) > area_limit:
            return False
    return topology_stable(
        [r for r in rings if not (ring_area(r) < 0 and is_local_hole(r))],
        [r for r in alternatives if not (ring_area(r) < 0 and is_local_hole(r))],
    )


def local_sensitivity(rings, variants, observations, routes, target, area_limit, domain=None):
    """按变化的物理片区核对证据，不把单个扰动洞的面积当成全域失败。"""
    from shapely.geometry import LineString, Point, Polygon
    from shapely.ops import unary_union

    audit = {"passed": False, "regions": [], "rollback_rings": [], "area_limit_m2": area_limit}
    if any(v is None for v in variants):
        audit["reason"] = "comparison_incomplete"
        return audit
    groups = []
    for index, group in enumerate([rings, *variants]):
        for ring in group:
            if ring_area(ring) >= 0:
                continue
            poly = Polygon(ring)
            if not poly.is_valid:
                audit["reason"] = "invalid_comparison_geometry"
                return audit
            touching = [g for g in groups if any(poly.intersects(p) for _, _, p in g)]
            merged = [(index, ring, poly)]
            for g in touching:
                merged.extend(g)
                groups.remove(g)
            groups.append(merged)
    used = 0.0
    water = domain.xy_shape(domain.water) if domain else None
    for group in groups:
        by_variant = [[r for j, r, _ in group if j == i] for i in range(len(variants) + 1)]
        if all(topology_stable(by_variant[0], other) for other in by_variant[1:]):
            continue
        envelope = unary_union([p for _, _, p in group]).convex_hull
        coords = list(envelope.exterior.coords)[:-1]
        span = max(math.dist(a, b) for a in coords for b in coords)
        slow = [o for o in observations if o.protected and o.seconds > target + 30]
        anchored = all(any(p.covers(Point(o.x, o.y)) for o in slow) for _, _, p in group)
        fast = any(
            o.protected and o.seconds <= target and envelope.covers(Point(o.x, o.y))
            for o in observations
        )
        crossing = any(
            r.seconds <= target
            and len(r.path) >= 2
            and LineString(r.path).intersection(envelope).length > 1e-6
            for r in routes
        )
        bounded = envelope.area <= 1000 and span <= 60
        water_hit = water is not None and water.intersects(envelope)
        used += envelope.area
        passed = anchored and bounded and not (fast or crossing or water_hit)
        audit["regions"].append(
            {
                "ring_xy": coords,
                "area_m2": envelope.area,
                "span_m": span,
                "slow_supported": anchored,
                "fast_inside": fast,
                "path_crossing": crossing,
                "water_intersection": water_hit,
                "passed": passed,
            }
        )
        # 只对没有可靠慢点支撑的有界局部变化试撤销；硬冲突不借回退掩盖。
        if bounded and not (anchored or fast or crossing or water_hit):
            audit["rollback_rings"].append(coords)
    audit["changed_area_m2"] = used
    within_budget = used <= area_limit and len(audit["regions"]) <= 16
    audit["passed"] = within_budget and all(r["passed"] for r in audit["regions"])
    if not within_budget:
        audit["rollback_rings"] = []
    return audit
