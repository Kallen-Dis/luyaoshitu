"""射线与网格融合的统一主几何；不把存疑测时或未知片段当成已确认超时。"""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

from .geometry import METERS_PER_DEG_LAT, haversine_m, meters_per_deg_lng
from .localfield import (
    BOUNDARY_TOLERANCE_S,
    Observation,
    RouteBound,
    contour_field,
    fit_field,
    hole_evidence,
)
from .tin import Vertex, clip_mesh, inside_ring, ring_area

VERSION = "ray-path-v3.16"


def apply_validation_gate(estimate: dict, validation: dict | None) -> None:
    """区分完整抽样核验和基础可靠的融合估计；所有硬冲突仍阻止采用。"""
    validation = validation or {}
    quality = estimate.setdefault("quality", {})
    required = max(4, math.ceil(validation.get("requested", 0) * 0.75))
    checked = validation.get("checked", 0)
    coverage = validation.get("coverage") or {}
    significant = sum(
        (p.get("verdict") == "conflict" or p.get("path_upper_conflict"))
        and p.get("route_s") is not None
        and abs(p["route_s"] - estimate.get("target_s", 900)) > BOUNDARY_TOLERANCE_S
        for p in validation.get("points", [])
    )
    quality.update(
        hole_validation_complete=bool(
            coverage.get("hole_validation_complete", not quality.get("holes"))
        ),
        validation_required=required,
        validation_checked=checked,
        validation_insufficient=checked < required,
        validation_disagreements=validation.get("disagreements", 0),
        validation_significant_conflicts=significant,
        validation_coverage_complete=bool(coverage.get("complete")),
        validation_plan_incomplete=coverage.get("plan_complete") is False,
        validation_unplanned_regions=len(coverage.get("unplanned_regions") or []),
        validation_budget_shortfall=(coverage.get("budget") or {}).get("shortfall", 0),
    )
    structurally_ready = quality.setdefault("structural_ready", bool(quality.get("ready")))
    hard_conflicts = (
        quality.get("mesh_topology_stable") is False
        or quality.get("solver_converged") is False
        or (quality.get("origin_preflight") or {}).get("passed") is False
    ) or any(
        quality.get(k)
        for k in (
            "geometry_error",
            "constraint_conflicts",
            "measurement_conflicts",
            "origin_conflicts",
            "domain_route_conflicts",
            "geographic_risk_unresolved",
            "path_violations",
            "invalid_path_segments",
            "incomplete_paths",
            "clipped_paths",
            "boundary_clipped",
            "unsupported_holes",
            "unsupported_components",
            "route_response_conflicts",
            "blocked_observations",
        )
    )
    complete = (
        structurally_ready
        and not hard_conflicts
        and not (
            checked < required
            or significant
            or not quality["validation_coverage_complete"]
            or quality["validation_plan_incomplete"]
            or quality.get("preflight_blocked")
            or quality.get("geographic_verification_pending")
            or quality.get("water_path_conflicts")
            or quality.get("local_hole_estimate_count")
            or quality.get("localized_topology_only")
            or (quality.get("holes") and not quality["hole_validation_complete"])
        )
    )
    # 采用估计与完成独立核验分开。仅放宽抽样覆盖/预算，不放宽真实约束、位置或水域冲突。
    estimation_safe = (
        structurally_ready
        and quality.get("solver_converged") is True
        and quality.get("mesh_topology_stable") is True
        and (quality.get("origin_preflight") or {}).get("passed") is True
        and quality.get("protected_routes", 0) >= 4
        and not significant
        and not hard_conflicts
    )
    quality["ready"] = bool(complete or estimation_safe)
    quality["validation_status"] = (
        "verified" if complete else "estimated" if estimation_safe else "rejected"
    )
    quality["sampling_pending"] = bool(estimation_safe and not complete)


def _clip_convex(fragment, boundary):
    out = fragment
    if ring_area(boundary) < 0:
        boundary = list(reversed(boundary))
    for a, b in zip(boundary, [*boundary[1:], boundary[0]], strict=True):
        if not out:
            break

        def side(point, a=a, b=b):
            return (b[0] - a[0]) * (point[1] - a[1]) - (b[1] - a[1]) * (point[0] - a[0])

        clipped = []
        previous = out[-1]
        pv = side(previous)
        for current in out:
            cv = side(current)
            if (pv >= 0) != (cv >= 0):
                ratio = pv / (pv - cv)
                clipped.append(
                    (
                        previous[0] + ratio * (current[0] - previous[0]),
                        previous[1] + ratio * (current[1] - previous[1]),
                    )
                )
            if cv >= 0:
                clipped.append(current)
            previous, pv = current, cv
        out = clipped
    return out


def _overlap_ray_area(fragments, ray):
    """射线圈相对中心为星形；扇形三角片互不重叠，逐片求交得到精确模型面积。"""
    fans = [[(0.0, 0.0), a, b] for a, b in zip(ray, [*ray[1:], ray[0]], strict=True)]
    area = 0.0
    for fragment in fragments:
        for fan in fans:
            if abs(ring_area(fan)) < 1e-6:
                continue
            intersection = _clip_convex(fragment, fan)
            if len(intersection) >= 3:
                area += abs(ring_area(intersection))
    return area


def rings_geometry(rings, center):
    """有向外环/孔洞组装完整 MultiPolygon，坐标使用 BD09 [lng, lat]。"""
    outer = [r for r in rings if ring_area(r) > 0]
    holes = [r for r in rings if ring_area(r) < 0]
    groups = [[r] for r in outer]
    for hole in holes:
        matches = [i for i, ring in enumerate(outer) if inside_ring(*hole[0], ring)]
        if not matches:
            raise ValueError("融合孔洞没有对应外环")
        owner = min(matches, key=lambda i: abs(ring_area(outer[i])))
        groups[owner].append(hole)
    mpl = meters_per_deg_lng(center[0])
    coordinates = []
    for group in groups:
        face = []
        for ring in group:
            points = [
                [round(center[1] + x / mpl, 8), round(center[0] + y / METERS_PER_DEG_LAT, 8)]
                for x, y in ring
            ]
            face.append([*points, points[0]])
        coordinates.append(face)
    return {"type": "MultiPolygon", "coordinates": coordinates}


def fusion_time(point, target):
    """路线位置核验后采用自身耗时；其余有限测距配模型等待作为估计，阈值两侧同口径。"""
    if point.off_network or point.blocked:
        return None
    if point.route_authoritative:
        return point.route_s
    return point.eff_s


def estimate_fusion(checker, ray_samples, excluded=None):
    """数值/拓扑失败也提供可审计的拒绝结果，不能使整次社区体检失败。"""
    try:
        return _estimate_fusion(checker, ray_samples, excluded)
    except ValueError as exc:
        ray = [checker.xy(lat, lng) for lat, lng in checker.polygon]
        area = abs(ring_area(ray)) / 1e6
        return {
            "version": VERSION,
            "target_s": checker.cfg.target_s,
            "geometry": rings_geometry(
                [ray if ring_area(ray) > 0 else list(reversed(ray))], checker.center
            ),
            "quality": {
                "ready": False,
                "geometry_error": str(exc),
                "missing_observations": 0,
                "constraint_conflicts": 0,
                "protected_routes": 0,
                "computational_spacing_m": 50,
            },
            "unknown_geometry": {"type": "MultiPolygon", "coordinates": []},
            "area_km2": area,
            "unknown_km2": 0,
            "unknown_inside_ray_km2": 0,
            "added_area_km2": 0,
            "omitted_ray_area_km2": 0,
            "pending_points": [],
            "measured_points": [],
            "rings": [],
            "time_field": {"vertices": [], "triangles": []},
            "barrier_triangles": 0,
            "triangles": 0,
            "basis": "融合计算未形成有效几何，保留射线结果。",
        }


def _estimate_fusion(checker, ray_samples, excluded=None):
    cfg = checker.cfg
    target = cfg.target_s
    excluded = excluded or set()
    by_position: dict[tuple[int, int], Vertex] = {}

    def put(lat, lng, value, distance):
        x, y = checker.xy(lat, lng)
        key = (round(x * 100), round(y * 100))
        by_position[key] = Vertex(
            x, y, value if value is not None and math.isfinite(value) else None, distance
        )

    put(*checker.center, 0.0, 0.0)
    for lat, lng, value, distance in ray_samples:
        put(lat, lng, value, distance)
    pending = []
    measurements = []
    observations = []
    routes = []
    starts = []
    incomplete_paths = 0
    excluded_origins = 0
    rejected_positions = 0
    domain_route_conflicts = 0
    for point in sorted(checker.probes.values(), key=lambda p: p.key):
        if not point.measured or point.kind == "validation" or point.key in excluded:
            continue
        relocated = point.observed_location is not None and not point.blocked
        domain = checker.cfg.domain
        pos = point.observed_location or (point.lat, point.lng)
        if domain is not None and not domain.allowed(*pos):
            domain_route_conflicts += int(relocated or point.route_authoritative)
            continue
        lat, lng = point.observed_location if relocated else (point.lat, point.lng)
        value = point.route_s if relocated else fusion_time(point, target)
        from .routequality import effective_origin, path_prefix_usable, same_origin

        route_evidence = relocated or point.route_authoritative
        origin_ok = not route_evidence or same_origin(point, checker.center)
        position_ok = not route_evidence or (
            point.route_position_consistent
            and point.route_geometry_complete
            and point.route_position_basis == "step_and_path"
        )
        excluded_origins += int(not origin_ok)
        rejected_positions += int(not position_ok)
        if not origin_ok or not position_ok:
            value = None
        distance = point.route_m if relocated or point.route_authoritative else point.dist_m
        put(lat, lng, value, distance)
        protected = False
        if value is not None and math.isfinite(value):
            x, y = checker.xy(lat, lng)
            protected = relocated or point.route_authoritative
            observations.append(
                Observation(x, y, value, 80.0 if protected else 6.0, protected, distance)
            )
            if protected and effective_origin(point) is not None:
                starts.append(effective_origin(point))
            if protected and point.route_path and path_prefix_usable(point, checker.center):
                if point.route_geometry_complete:
                    routes.append(
                        RouteBound(tuple(checker.xy(*p) for p in point.route_path), value, distance)
                    )
                else:
                    incomplete_paths += 1
        measurements.append(
            {
                "lat": round(lat, 6),
                "lng": round(lng, 6),
                "raw_s": point.raw_s,
                "route_response_s": point.route_s,
                "time_s": value,
                "distance_m": distance,
                "matrix_distance_m": point.dist_m,
                "actual_origin": list(effective_origin(point)) if effective_origin(point) else None,
                "path_start": list(point.route_start) if point.route_start else None,
                "origin_basis": point.route_origin_basis,
                "fetched_at": point.route_fetched_at,
                "cache_hit": point.route_cache_hit,
                "position_basis": point.route_position_basis,
                "origin_accepted": origin_ok,
                "position_accepted": position_ok,
                "time_basis": "quarantined_route"
                if route_evidence and (not origin_ok or not position_ok)
                else "route_duration_includes_model_wait"
                if protected
                else "matrix_plus_model_wait",
                "source": "relocated_route"
                if relocated and haversine_m(point.lat, point.lng, lat, lng) > 0.01
                else "own_route"
                if point.route_authoritative
                else point.delay_source,
                **(
                    {"requested_lat": round(point.lat, 6), "requested_lng": round(point.lng, 6)}
                    if relocated and haversine_m(point.lat, point.lng, lat, lng) > 0.01
                    else {}
                ),
            }
        )
        if value is None:
            pending.append(
                {
                    "lat": round(point.lat, 6),
                    "lng": round(point.lng, 6),
                    "size_m": point.size_m,
                    "reason": "接口冲突、端点偏离或等待/围挡尚未核实",
                }
            )
    ray = [checker.xy(lat, lng) for lat, lng in checker.polygon]
    samples = [
        (*checker.xy(lat, lng), value, distance) for lat, lng, value, distance in ray_samples
    ]
    # 数值上相同的实际起点才共用路径上限；地理距离近不证明道路相通。
    origin_anchor = checker.center
    from .holepolicy import computation_clues

    clues = computation_clues(checker)
    origin_conflicts = sum(haversine_m(*origin_anchor, *p) > 1 for p in starts)
    vertices, triangles, quality = fit_field(
        ray,
        samples,
        observations,
        target,
        routes=routes,
        rollback_xy=getattr(checker, "rollback_xy", ()),
        clues=clues,
        domain=checker.cfg.domain,
    )
    quality["origin_conflicts"] = origin_conflicts
    quality["domain_route_conflicts"] = domain_route_conflicts
    quality["excluded_origin_routes"] = excluded_origins
    quality["rejected_position_routes"] = rejected_positions
    quality["origin_policy"] = "fixed_requested_center"
    quality["legacy_position_routes"] = sum(
        p["position_basis"] != "step_and_path" and p["route_response_s"] is not None
        for p in measurements
    )
    quality["incomplete_paths"] = incomplete_paths
    if (
        origin_conflicts
        or domain_route_conflicts
        or incomplete_paths
        or ((excluded_origins or rejected_positions) and not routes)
    ):
        quality["ready"] = False
    quality["missing_observations"] = len(pending)
    quality["basis"] = "缺测保留来源记录，周围有证据时采用基础估计；计算格距不等于实测精度。"
    if sum(p.blocked and p.inside for p in checker.probes.values()):
        quality["ready"] = False
        quality["blocked_observations"] = True
    mesh = contour_field(vertices, triangles, target)
    thresholds = [t for t in (300, 600, target) if t <= target]
    preliminary = {t: contour_field(vertices, triangles, t) for t in sorted(set(thresholds))}
    stable = {}
    sensitivity = {t: [] for t in preliminary}
    # 即使粗网格没有孔洞，也要检查加密是否凭空生出孔洞。
    if any(o.protected for o in observations):
        finer_v, finer_t, finer_q = fit_field(
            ray,
            samples,
            observations,
            target,
            routes=routes,
            refine=True,
            rollback_xy=getattr(checker, "rollback_xy", ()),
            clues=clues,
            domain=checker.cfg.domain,
        )
        quality["refinement_quality"] = finer_q
        if finer_q["ready"]:
            stable = {t: contour_field(finer_v, finer_t, t).rings for t in preliminary}
    else:
        stable = {t: m.rings for t, m in preliminary.items()}
    if any(ring_area(r) < 0 for m in preliminary.values() for r in m.rings) or any(
        ring_area(r) < 0 for rings in stable.values() for r in rings
    ):
        for scale in (0.8, 1.2):
            sv, st, sq = fit_field(
                ray,
                samples,
                observations,
                target,
                routes=routes,
                rollback_xy=getattr(checker, "rollback_xy", ()),
                clues=clues,
                domain=checker.cfg.domain,
                support_scale=scale,
            )
            for t in preliminary:
                sensitivity[t].append(contour_field(sv, st, t).rings if sq["ready"] else [])
    topology = {
        str(t): hole_evidence(
            m.rings,
            observations,
            t,
            routes,
            stable.get(t),
            clues=clues,
            domain=checker.cfg.domain,
            sensitivity_rings=sensitivity[t],
        )
        for t, m in preliminary.items()
    }
    quality.update(topology[str(target)])
    from .holepolicy import topology_stable

    for t in preliminary:
        topology[str(t)]["mesh_topology_stable"] = topology_stable(
            preliminary[t].rings, stable.get(t)
        ) and all(topology_stable(preliminary[t].rings, variant) for variant in sensitivity[t])
    from .holepolicy import is_local_hole, localized_topology_stable

    strict_stability = topology[str(target)]["mesh_topology_stable"]
    area_limit = min(1000.0, abs(ring_area(ray)) * 0.001)
    local_records = [
        r
        for r in quality["hole_records"]
        if not r["supported"]
        and is_local_hole(r["ring_xy"])
        and r["slow_points"] >= 1
        and not r["path_crossings"]
        and not r["fast_inside"]
    ]
    if sum(r["area_m2"] for r in local_records) > area_limit or len(local_records) > 16:
        local_records = []
    quality["strict_unsupported_holes"] = quality["unsupported_holes"]
    quality["local_hole_estimates"] = local_records
    quality["local_hole_estimate_count"] = len(local_records)
    quality["local_hole_policy"] = {
        "max_area_m2": 400,
        "max_span_m": 40,
        "total_area_limit_m2": area_limit,
        "max_count": 16,
    }
    quality["geographic_verification_pending"] = checker.cfg.domain is None and bool(
        local_records or quality.get("micro_hole_regularization", {}).get("filled_count")
    )
    quality["unsupported_holes"] -= len(local_records)
    quality["strict_mesh_topology_stable"] = strict_stability
    local_stability = localized_topology_stable(
        preliminary[target].rings, stable.get(target), area_limit
    ) and all(
        localized_topology_stable(preliminary[target].rings, variant, area_limit)
        for variant in sensitivity[target]
    )
    quality["mesh_topology_stable"] = strict_stability or local_stability
    quality["localized_topology_only"] = bool(local_stability and not strict_stability)
    quality["mesh_stability_status"] = (
        "passed"
        if quality["mesh_topology_stable"]
        else "incomplete"
        if quality.get("refinement_quality", {}).get("ready") is False
        else "changed"
    )
    for record in quality["hole_records"]:
        record["ring"] = rings_geometry([list(reversed(record["ring_xy"]))], checker.center)[
            "coordinates"
        ][0][0]
    quality["origin_preflight"] = getattr(checker, "origin_preflight", None)
    quality["clue_constraints"] = clues
    if getattr(checker, "origin_preflight", {}).get("passed") is False:
        quality["ready"] = False
    quality["inner_topology"] = topology
    components = [r for r in mesh.rings if ring_area(r) > 0 and not inside_ring(0, 0, r)]
    from shapely.geometry import LineString, Polygon

    reachable_paths = [
        LineString(route.path)
        for route in routes
        if route.seconds <= target and len(route.path) >= 2
    ]
    # 可信路线沿途也证明可达，不要求路线恰好结束在该片区内。
    quality["unsupported_component_rings"] = [
        r
        for r in components
        if quality["path_violations"]
        or not any(Polygon(r).intersects(path) for path in reachable_paths)
    ]
    quality["unsupported_components"] = len(quality["unsupported_component_rings"])
    quality["unsupported_inner_holes"] = sum(
        v["unsupported_holes"] for k, v in topology.items() if float(k) < target
    )
    if (
        quality["unsupported_holes"]
        or quality["unsupported_components"]
        or not quality["mesh_topology_stable"]
    ):
        quality["ready"] = False
    geometry = rings_geometry(mesh.rings, checker.center)
    unknown = rings_geometry(mesh.unknown_rings, checker.center)
    inner = []
    for minutes in (5, 10):
        if minutes * 60 >= target:
            continue
        if (
            topology[str(minutes * 60)]["unsupported_holes"]
            or not topology[str(minutes * 60)]["mesh_topology_stable"]
        ):
            continue
        result = clip_mesh(mesh, minutes * 60)
        inner_geometry = rings_geometry(result.rings, checker.center)
        faces = inner_geometry["coordinates"]
        inner.append(
            {
                "minutes": minutes,
                "geometry": inner_geometry,
                "coordinates": faces[0][0] if faces else [],
            }
        )
    ray = [checker.xy(lat, lng) for lat, lng in checker.polygon]
    unknown_inside = _overlap_ray_area(mesh.unknown_fragments, ray)
    overlap = _overlap_ray_area(mesh.fragments, ray)
    return {
        "version": VERSION,
        "origin_anchor": list(origin_anchor),
        "training_paths": [
            {
                "seconds": r.seconds,
                "path": [
                    [
                        checker.center[1] + x / checker.mpl,
                        checker.center[0] + y / METERS_PER_DEG_LAT,
                    ]
                    for x, y in r.path
                ],
            }
            for r in routes
        ],
        "target_s": target,
        "quality": quality,
        "geometry": geometry,
        "unknown_geometry": unknown,
        "area_km2": round(mesh.area_m2 / 1e6, 6),
        "unknown_km2": round(mesh.unknown_m2 / 1e6, 6),
        "unknown_inside_ray_km2": round(max(0.0, unknown_inside) / 1e6, 6),
        "added_area_km2": round(max(0.0, mesh.area_m2 - overlap) / 1e6, 6),
        "omitted_ray_area_km2": round(max(0.0, abs(ring_area(ray)) - overlap) / 1e6, 6),
        "pending_points": pending,
        "measured_points": measurements,
        "time_field": {
            "vertices_xy": [[v.x, v.y] for v in mesh.field_vertices],
            "vertices": [
                [
                    round(checker.center[1] + v.x / checker.mpl, 8),
                    round(checker.center[0] + v.y / METERS_PER_DEG_LAT, 8),
                    v.t,
                ]
                for v in mesh.field_vertices
            ],
            "triangles": mesh.valid_triangles,
        },
        "rings": inner,
        "barrier_triangles": mesh.barrier_triangles,
        "triangles": mesh.triangles,
        "basis": "射线提供基础耗时，真实终点局部插点，完整可信路线提供沿途上限；"
        "阈值两侧的模型临界带补测并重算，补测与冻结后的新点验收分开；"
        "慢点局部生效，强路线跳变限制修正耦合，"
        "不定位障碍。5/10/15 分钟共享同一耗时场，绿色表示模型估计。",
    }


def apply_fusion(feature: dict[str, Any], checked: dict[str, Any]) -> bool:
    """有证据且稳定的候选分级采用；候选仍需完整重算统计后发布。"""
    estimate = checked.get("fusion")
    props = feature["properties"]
    holes = 0
    if estimate:
        apply_validation_gate(estimate, checked.get("validation"))
        # 默认范围的选择以真实几何为准，不能依赖可能缺失或过时的 holes 标记。
        # GeoJSON 每个面的第一环是外环，后续环是孔洞；多片外环与 5/10 分钟圈不计。
        geometry = estimate.get("geometry") or {}
        coordinates = geometry.get("coordinates") or []
        faces = (
            [coordinates]
            if geometry.get("type") == "Polygon"
            else coordinates
            if geometry.get("type") == "MultiPolygon"
            else []
        )
        holes = sum(bool(ring) for face in faces for ring in face[1:])
        estimate.setdefault("quality", {})["main_geometry_holes"] = holes
    if (
        not estimate
        or not estimate["geometry"].get("coordinates")
        or not estimate.get("quality", {}).get("ready", True)
        or estimate.get("quality", {}).get("mesh_topology_stable") is False
        or (
            holes
            and (
                (
                    estimate.get("quality", {}).get("supported_holes", 0)
                    + (
                        estimate.get("quality", {}).get("local_hole_estimate_count", 0)
                        if estimate.get("quality", {}).get("validation_status") == "estimated"
                        else 0
                    )
                )
                != holes
                or (
                    not estimate.get("quality", {}).get("hole_validation_complete", False)
                    and estimate.get("quality", {}).get("validation_status") != "estimated"
                )
            )
        )
    ):
        props["geometry_method"] = "ray"
        quality = (estimate or {}).get("quality", {})
        reasons = []
        if quality.get("domain_route_conflicts"):
            reasons.append("可靠步行终点与水域边界冲突，需核实桥面或岸线，未静默删除路线证据")
        if quality.get("clipped_paths"):
            reasons.append("路线经过水域或超出计算范围，需核实桥面、岸线或计算边界")
        if quality.get("geographic_risk_unresolved"):
            reasons.append("采样位置偏移且缺少水域边界，地理范围风险尚未排除")
        if quality.get("mesh_topology_stable") is False:
            reasons.append(
                "加密检查未完成，详见加密求解约束记录"
                if quality.get("mesh_stability_status") == "incomplete"
                else "计算网格或影响半径变化后范围拓扑不稳定"
            )
        if holes and (
            quality.get("supported_holes", 0) != holes
            or not quality.get("hole_validation_complete")
        ):
            reasons.append(f"融合主范围含 {holes} 个内部孔洞，尚未完成逐孔证据及独立核验")
        if (quality.get("origin_preflight") or {}).get("passed") is False:
            reasons.append(
                (quality["origin_preflight"].get("reason") or "起点预检未通过")
                + "，已停止后续融合补测"
            )
        if quality.get("validation_budget_shortfall"):
            reasons.append(
                f"完成主目标验收计划至少还缺 {quality['validation_budget_shortfall']} 次路线请求"
            )
        if quality.get("validation_unplanned_regions"):
            reasons.append(
                f"{quality['validation_unplanned_regions']} 个修正片区未找到独立验收目标"
            )
        if quality.get("validation_insufficient"):
            reasons.append(
                f"留出点核验仅完成 {quality['validation_checked']} 个"
                f"（至少需 {quality['validation_required']} 个）"
            )
        if quality.get("validation_significant_conflicts"):
            reasons.append("留出点存在超过临界容差的圈内外分歧")
        if not quality.get("validation_coverage_complete"):
            reasons.append("关键修正片区或方向的核验覆盖不足")
        if quality.get("unsupported_holes") or quality.get("unsupported_inner_holes"):
            reasons.append("内部孔洞缺少可靠超时证据")
        if quality.get("origin_conflicts"):
            reasons.append("路线实际起点不一致")
        if quality.get("excluded_origin_routes") and not quality.get("protected_routes"):
            reasons.append("已有路线起点与输入中心不一致，已在拟合前排除，尚无同起点可靠路线")
        if quality.get("rejected_position_routes"):
            reasons.append("部分路线位置元数据缺失或不一致，已在拟合前隔离")
        if quality.get("route_response_conflicts"):
            reasons.append("超时路线响应与已知快路径或同位置响应冲突")
        if quality.get("measurement_conflicts"):
            reasons.append("模型与已接纳路线测时存在明确分类分歧，不能通过撤销修正隐藏")
        if quality.get("preflight_blocked"):
            reasons.append("候选未通过发布前预审，未新增独立验收请求")
        if (
            quality.get("path_violations")
            or quality.get("invalid_path_segments")
            or quality.get("incomplete_paths")
        ):
            reasons.append("路径上限存在冲突或路线几何不完整")
        if quality.get("unsupported_components"):
            reasons.append("孤立可达片区缺少完整路径支持")
        if quality.get("geometry_error"):
            reasons.append("融合几何未通过闭合与拓扑检查")
        props["fusion_fallback"] = (
            "；".join(reasons) or "局部校正未通过观测一致性或计算域检查"
        ) + "，保留射线估计范围"
        props["fusion_review"] = {
            "version": VERSION,
            "quality": deepcopy((estimate or {}).get("quality", {})),
            "validation": deepcopy(checked.get("validation")),
            "adoption": deepcopy((estimate or {}).get("adoption")),
        }
        return False
    props.pop("fusion_fallback", None)
    props.pop("fusion_review", None)
    original = deepcopy(feature["geometry"])
    props["fusion"] = {
        **estimate,
        "target_s": checked["target_s"],
        "ray_geometry": original,
        "ray_area_km2": props["area_km2"],
        "ray_statistics": {
            "score": (props.get("report") or {}).get("total"),
            "blind_ratio": (props.get("blindspots") or {}).get("blind_ratio"),
            "blind_count": (props.get("blindspots") or {}).get("blind_count"),
        },
        "status": "partial" if estimate["pending_points"] else "estimated",
        "validation": checked.get("validation"),
        "completion": checked.get("completion"),
        "notes": [
            "这是基于路网采样的融合估计，不代表所有门口均已实测。",
            "缺测不直接挖孔；无直接观测的位置属于模型估计。有位置核验的路线采用自身耗时。",
            "阈值附近 30 秒内的路线保留原始测时参与拟合，不强制圈内外分类；"
            "内部孔洞需有分散的超时路线点及周边可达证据。30 秒是工程容差，不是实测误差保证。",
            "仅在可靠路线证据附近进行局部校正；其他位置沿用射线估计，并不代表已逐点核实。",
            "端点偏离 10 至 150 米的路线，测时观测放回实际终点，不贴到原格心；"
            "留出核验还要求端点没有偏离目标片区且不与拟合样本重合。",
            "灰色区域判断居民步行 1 公里能否到达设施；"
            "红色超时证据判断中心到采样点是否超过时间阈值。",
            "面积来自融合几何；方向均衡与路径效率仍来自射线和路线采样。",
        ],
    }
    feature["geometry"] = deepcopy(estimate["geometry"])
    props["geometry_method"] = "fusion"
    props["algorithm_version"] = VERSION
    props["area_km2"] = estimate["area_km2"]
    props["area_m2"] = round(estimate["area_km2"] * 1e6, 2)
    props["rings"] = estimate["rings"]
    return True


async def publish_fusion(feature: dict, checked: dict, rebuild) -> bool:
    """副本完成统计后才发布；异常或取消都不会留下新几何搭配旧统计。"""
    candidate = deepcopy(feature)
    if not apply_fusion(candidate, checked):
        for key in ("geometry_method", "fusion_fallback", "fusion_review"):
            if key in candidate["properties"]:
                feature["properties"][key] = candidate["properties"][key]
        return False
    try:
        await rebuild(candidate)
    except Exception as exc:
        feature["properties"]["fusion_fallback"] = (
            f"融合范围统计未完成（{type(exc).__name__}），保留射线估计范围"
        )
        feature["properties"]["fusion_review"] = {
            "version": VERSION,
            "quality": deepcopy(checked["fusion"]["quality"]),
            "validation": deepcopy(checked.get("validation")),
            "publication_failed": True,
        }
        return False
    # 主结果已经保存融合元数据；辅助层不再重复携带整个耗时场。
    (candidate["properties"].get("gridcheck") or {}).pop("fusion", None)
    feature.clear()
    feature.update(candidate)
    return True


def field_time(field: dict, lat: float, lng: float) -> float | None:
    """格心提示与热力使用同一耗时场；共享边取可达一侧，保留真正空档。"""
    best = None
    vertices = field.get("vertices", [])
    for indices in field.get("triangles", []):
        a, b, c = [vertices[i] for i in indices]
        denominator = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
        if abs(denominator) < 1e-20 or any(p[2] is None for p in (a, b, c)):
            continue
        u = ((b[1] - c[1]) * (lng - c[0]) + (c[0] - b[0]) * (lat - c[1])) / denominator
        v = ((c[1] - a[1]) * (lng - c[0]) + (a[0] - c[0]) * (lat - c[1])) / denominator
        w = 1 - u - v
        if min(u, v, w) >= -1e-8:
            value = u * a[2] + v * b[2] + w * c[2]
            best = value if best is None else min(best, value)
    return best


def sync_heat_measurements(blindspots: dict, fusion: dict | None) -> None:
    """热力和点位提示使用同一场，单点原观测另存，避免两种口径互相矛盾。"""
    if not fusion:
        return
    measurements = {
        (round(p["lat"], 6), round(p["lng"], 6)): p for p in fusion.get("measured_points", [])
    }
    for cell in blindspots.get("cells", []):
        for item in [cell, *(cell.get("quarters") or [])]:
            point = measurements.get((round(item["lat"], 6), round(item["lng"], 6)))
            if fusion.get("time_field"):
                value = field_time(fusion["time_field"], item["lat"], item["lng"])
                item["reach_s"] = round(value, 2) if value is not None else None
                item["reach_source"] = "fusion_interpolated" if value is not None else "unmeasured"
                item["reach_observed_s"] = point["time_s"] if point else None
            if point is None:
                continue
            if not fusion.get("time_field"):
                item["reach_s"] = round(point["time_s"]) if point["time_s"] is not None else None
                item["reach_source"] = point["source"]
            item["reach_raw_s"] = round(point["raw_s"]) if point["raw_s"] is not None else None
            distance = point.get("matrix_distance_m", item.get("reach_m"))
            item["reach_m"] = round(distance) if distance is not None else None
            if point["source"] == "own_route":
                item["reach_route_m"] = point["distance_m"]
