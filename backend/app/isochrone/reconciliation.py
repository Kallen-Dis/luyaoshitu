"""同次体检内有限次纠错：旧候选及其反例留档，新候选必须另取新点。"""

from copy import deepcopy

from .adoption import candidate_hash, prepare_fusion
from .fusion import apply_validation_gate, estimate_fusion, field_time
from .localfield import BOUNDARY_TOLERANCE_S


def unresolved_evidence(fusion, records):
    """计量小偏差不抹掉已知反例；处理范围再小也必须尊重可靠测点分类。"""
    out = []
    for record in records:
        lng, lat = record["actual_endpoint"]
        value = field_time(fusion["time_field"], lat, lng)
        observed = record["route_s"]
        if value is None or (
            abs(observed - fusion["target_s"]) > BOUNDARY_TOLERANCE_S
            and (value <= fusion["target_s"]) != (observed <= fusion["target_s"])
        ):
            out.append({"lat": lat, "lng": lng, "route_s": observed, "predicted_s": value})
    return out


def repair_scope(before, after, records, chk):
    """检查改动是否仍落在新增证据附近；路线约束允许沿已有完整路径生效。"""
    from shapely.errors import GEOSException
    from shapely.geometry import LineString, Point, shape
    from shapely.ops import transform, unary_union

    def xy(lng, lat, z=None):
        return (lng - chk.center[1]) * chk.mpl, (lat - chk.center[0]) * 111320

    envelopes = []
    for record in records:
        p = chk.probes.get(tuple(record["probe_key"]))
        lng, lat = record["actual_endpoint"]
        envelopes.append(Point(chk.xy(lat, lng)).buffer(175))
        if p and p.route_path and len(p.route_path) > 1:
            envelopes.append(LineString([chk.xy(*pos) for pos in p.route_path]).buffer(100))
    old = transform(xy, shape(before["geometry"]))
    new = transform(xy, shape(after["geometry"]))
    limit = min(1000.0, old.area * 0.001)
    if not old.is_valid or not new.is_valid:
        return {"passed": False, "reason": "invalid_geometry", "limit_m2": limit}
    try:
        delta = old.symmetric_difference(new)
        outside = delta.difference(unary_union(envelopes)).area
    except GEOSException:
        return {"passed": False, "reason": "geometry_operation_failed", "limit_m2": limit}
    return {
        "changed_m2": delta.area,
        "outside_evidence_m2": outside,
        "limit_m2": limit,
        "passed": outside <= max(1.0, limit),
    }


async def reconcile_fusion(chk, lattice, leaves, fusion, samples):
    from .gridcheck import _validate_fusion

    history, known = [], []
    current = fusion
    pending_scope = None
    for round_id in range(chk.cfg.repair_rounds + 1):
        chk.validation_round = round_id
        candidate = prepare_fusion(chk, lattice, leaves, current, samples)
        # 预检会做局部回退，必须检查真正被冻结的候选，而非预检前的临时面。
        if pending_scope is not None:
            before, promoted = pending_scope
            scope = repair_scope(before, candidate, promoted, chk)
            history[-1]["repair_scope"] = scope
            if not scope["passed"]:
                candidate["quality"]["repair_scope_exceeded"] = True
                empty = {
                    "checked": 0,
                    "requested": 0,
                    "points": [],
                    "coverage": {"complete": False},
                }
                apply_validation_gate(candidate, empty)
                candidate["repair"] = {"rounds": history, "evidence_count": len(known)}
                return candidate, empty
        validation = await _validate_fusion(chk, lattice, leaves, {"fusion": candidate}, samples)
        apply_validation_gate(candidate, validation)
        conflicts = [
            p
            for p in validation.get("points", [])
            if p.get("significant_conflict") or p.get("path_upper_conflict")
        ]
        history.append(
            {
                "round": round_id,
                "candidate_hash": candidate_hash(candidate),
                "validation": deepcopy(validation),
            }
        )
        if known:
            remaining = unresolved_evidence(candidate, known)
            candidate["quality"]["repair_unresolved_conflicts"] = len(remaining)
            candidate["quality"]["repair_verification_pending"] = (
                validation.get("checked", 0) < 2
                or len(
                    {
                        p.get("sector")
                        for p in validation.get("points", [])
                        if p.get("verdict") != "unknown"
                    }
                )
                < 2
            )
            candidate["repair"] = {
                "rounds": history,
                "evidence_count": len(known),
                "unresolved": remaining,
            }
            apply_validation_gate(candidate, validation)
        if (
            not conflicts
            or round_id >= chk.cfg.repair_rounds
            or candidate["quality"].get("preflight_blocked")
            or any(p.get("path_upper_conflict") for p in conflicts)
            or candidate["quality"].get("repair_scope_exceeded")
        ):
            candidate.setdefault("repair", {"rounds": history, "evidence_count": len(known)})
            return candidate, validation

        # 将这一轮所有有效观测转为下一候选的训练数据，而不是只吸收容易通过的点。
        promoted = [
            p
            for p in validation["points"]
            if p["verdict"] != "unknown" and p.get("probe_key") and p.get("route_s") is not None
        ]
        if not promoted:
            candidate["repair"] = {"rounds": history, "evidence_count": len(known)}
            return candidate, validation
        for record in promoted:
            chk.probes[tuple(record["probe_key"])].kind = "repair_evidence"
        for p in chk.probes.values():
            if p.kind == "validation":
                p.kind = "validation_history"
        known.extend(promoted)
        chk.retired_validation_xy = [
            chk.xy(p["actual_endpoint"][1], p["actual_endpoint"][0]) for p in known
        ]
        # 补入可信终点及完整路径后重算同一个场，不缓冲/拼接显示多边形。
        current = estimate_fusion(chk, samples)
        pending_scope = (candidate, promoted)
    raise AssertionError("unreachable reconciliation round")
