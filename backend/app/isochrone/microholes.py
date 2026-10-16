"""候选冻结前的有界小孔洞正则化；改耗时场，不删除显示内环冒充通过。"""

import math

from shapely.errors import ShapelyError
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from .tin import Vertex, ring_area

MAX_AREA_M2 = 100.0
MAX_SPAN_M = 20.0
MAX_EXCESS_S = 100.0
MAX_FILLED_HOLES = 16


def _surface(rings):
    outers = [Polygon(r) for r in rings if ring_area(r) > 0]
    holes = [[] for _ in outers]
    for ring in (r for r in rings if ring_area(r) < 0):
        p = Polygon(ring)
        owners = [i for i, outer in enumerate(outers) if outer.covers(p.representative_point())]
        if not owners:
            raise ValueError("孔洞缺少外环")
        holes[min(owners, key=lambda i: outers[i].area)].append(ring)
    return unary_union(
        [Polygon(p.exterior.coords, hs) for p, hs in zip(outers, holes, strict=True)]
    )


def regularize_micro_holes(
    vertices,
    triangles,
    observations,
    target,
    *,
    domain=None,
    water_xy=None,
    lower=None,
    clues=(),
    area_limit=500.0,
):
    from .localfield import contour_field

    audit = {
        "policy": "bounded_field_micro_holes_v1",
        "max_area_m2": MAX_AREA_M2,
        "max_span_m": MAX_SPAN_M,
        "max_excess_s": MAX_EXCESS_S,
        "total_area_limit_m2": area_limit,
        "filled_area_m2": 0.0,
        "filled_count": 0,
        "adjusted_vertices": 0,
        "records": [],
        "basis": "微小插值孔洞的模型正则化，不是新增路线实测；水域及可靠超时证据不得覆盖。",
    }
    field = list(vertices)
    audit["geographic_verification_pending"] = domain is None
    holes = [r for r in contour_field(field, triangles, target).rings if ring_area(r) < 0]
    if not holes:
        return field, audit
    water = (
        water_xy if water_xy is not None else domain.xy_shape(domain.water) if domain else Polygon()
    )
    coverage = domain.xy_shape(domain.coverage) if domain else None
    for ring in sorted(holes, key=lambda r: (abs(ring_area(r)), min(r))):
        hole = Polygon(ring)
        area = hole.area
        record = {"ring_xy": ring, "area_m2": area, "filled": False}
        reason = None
        if area > MAX_AREA_M2:
            reason = "area_limit"
        else:
            hull = list(hole.convex_hull.exterior.coords)
            span = max(math.dist(a, b) for a in hull for b in hull)
            record["span_m"] = span
            if span > MAX_SPAN_M:
                reason = "span_limit"
        nearby = hole.buffer(1e-6)  # 与米坐标6位小数舍入口径相同。
        indices = (
            [
                i
                for i, v in enumerate(field)
                if v.t is not None and v.t > target and nearby.covers(Point(v.x, v.y))
            ]
            if reason is None
            else []
        )
        if reason is None:
            if (coverage is not None and not coverage.covers(hole)) or water.intersects(hole):
                reason = "geographic_exclusion"
            elif any(
                o.protected and o.seconds > target and nearby.covers(Point(o.x, o.y))
                for o in observations
            ):
                reason = "reliable_slow_observation"
            elif any(
                c.get("status") == "confirmed" and nearby.covers(Point(c["far"])) for c in clues
            ):
                reason = "confirmed_detour_evidence"
            elif not indices:
                reason = "no_adjustable_field_vertices"
            elif any(
                not math.isfinite(field[i].t) or field[i].t - target > MAX_EXCESS_S for i in indices
            ):
                reason = "time_adjustment_limit"
            elif lower is not None and any(lower[i] > target for i in indices):
                reason = "hard_lower_bound"
            elif (
                audit["filled_count"] >= MAX_FILLED_HOLES
                or audit["filled_area_m2"] + area > area_limit
            ):
                reason = "total_adjustment_limit"
        if reason is None:
            trial = list(field)
            for i in indices:
                v = trial[i]
                value = max(lower[i] if lower is not None else 0.0, target - 1e-6)
                trial[i] = Vertex(v.x, v.y, value, v.d)
            # 降低线性场节点只会扩大范围；扩大必须恰好位于该孔洞内，不能改变外边界。
            old = contour_field(field, triangles, target)
            new = contour_field(trial, triangles, target)
            try:
                before, after = _surface(old.rings), _surface(new.rings)
                safe = before.is_valid and after.is_valid
                if safe:
                    added = after.difference(before)
                    safe = (
                        abs((new.area_m2 - old.area_m2) - area) <= max(0.001, area * 1e-6)
                        and added.difference(nearby).area <= 1e-6
                        and hole.difference(added).area <= 0.001
                        and before.difference(after).area <= 1e-6
                    )
            except (ShapelyError, ValueError):
                safe = False
            if not safe:
                reason = "nonlocal_geometry_change"
            else:
                field = trial
                record.update(
                    filled=True,
                    adjusted_vertices=len(indices),
                    max_adjustment_s=max(vertices[i].t - target for i in indices),
                )
                audit["filled_count"] += 1
                audit["filled_area_m2"] += area
                audit["adjusted_vertices"] += len(indices)
        record["reason"] = reason or "bounded_model_regularization"
        audit["records"].append(record)
    return field, audit
