"""请求前审查修正的可验证性；只在耗时场中撤销，禁止按验收结果修补候选。"""

import hashlib
import json
from copy import deepcopy

from .fusion import estimate_fusion, field_time
from .localfield import BOUNDARY_TOLERANCE_S
from .tin import inside_ring
from .validation import validation_plan, validation_request_budget


def candidate_hash(fusion):
    # 孔洞能否发布取决于几何、证据及线索，冻结不能只保护耗时数组。
    frozen = {
        key: fusion.get(key)
        for key in (
            "version",
            "target_s",
            "time_field",
            "geometry",
            "origin_anchor",
            "measured_points",
            "training_paths",
        )
    }
    frozen["hole_records"] = fusion.get("quality", {}).get("hole_records", [])
    frozen["clue_constraints"] = fusion.get("quality", {}).get("clue_constraints", [])
    frozen["numerical_policy"] = {
        key: fusion.get("quality", {}).get(key)
        for key in (
            "mesh_topology_stable",
            "regularization",
            "prior_integral_weight",
            "micro_hole_regularization",
            "local_hole_policy",
            "local_hole_estimates",
            "strict_mesh_topology_stable",
            "localized_topology_only",
        )
    }
    return hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest()


def targets_hash(targets):
    return hashlib.sha256(json.dumps(targets, sort_keys=True).encode()).hexdigest()


def prepare_fusion(chk, lattice, leaves, fusion, samples):
    if any(p.kind == "validation" for p in chk.probes.values()):
        raise ValueError("候选验收开始后不能重新采用修正；须新建一轮候选与独立目标")
    fusion = deepcopy(fusion)
    rolled = set(getattr(chk, "rollback_xy", ()))
    history = []
    previous = None
    for iteration in range(3):
        targets, plan = validation_plan(chk, lattice, leaves, fusion, samples)
        budget = validation_request_budget(chk, targets)
        quality = fusion["quality"]
        measurement_conflicts = []
        for point in fusion.get("measured_points", []):
            if (
                point.get("time_basis") != "route_duration_includes_model_wait"
                or point.get("time_s") is None
            ):
                continue
            observed = point["time_s"]
            predicted = field_time(fusion["time_field"], point["lat"], point["lng"])
            if predicted is None or (
                abs(observed - chk.cfg.target_s) > BOUNDARY_TOLERANCE_S
                and (observed <= chk.cfg.target_s) != (predicted <= chk.cfg.target_s)
            ):
                measurement_conflicts.append(
                    {
                        "lat": point["lat"],
                        "lng": point["lng"],
                        "observed_s": observed,
                        "predicted_s": predicted,
                    }
                )
        quality["measurement_conflicts"] = len(measurement_conflicts)
        fatal = any(
            quality.get(k)
            for k in (
                "geometry_error",
                "constraint_conflicts",
                "measurement_conflicts",
                "origin_conflicts",
                "path_violations",
                "invalid_path_segments",
                "incomplete_paths",
                "clipped_paths",
                "boundary_clipped",
            )
        ) or (quality.get("excluded_origin_routes", 0) > 0 and not quality.get("protected_routes"))
        field = fusion["time_field"]
        xy = field.get("vertices_xy") or [chk.xy(v[1], v[0]) for v in field["vertices"]]
        # 抽样预算或选点不足只影响核验等级，不能撤销已有可靠修正。
        # 否则会把可靠快/慢点拉回射线先验，制造新的路径冲突。
        reject = set()
        new_nodes = {
            tuple(xy[i]) for r in plan["regions"] if r["id"] in reject for i in r["node_indices"]
        }
        # 不直接填孔：撤销涉及的场节点，重算后仍须满足全部可靠路线约束。
        if quality.get("unsupported_holes"):
            holes = [r["ring_xy"] for r in quality.get("hole_records", []) if not r["supported"]]
            for face in field["triangles"]:
                if any(inside_ring(*xy[i], ring) for i in face for ring in holes):
                    new_nodes.update(tuple(xy[i]) for i in face)
        # 没有可达路径支持的独立碎片撤销其局部修正，随后完整重算及核验。
        for ring in quality.get("unsupported_component_rings", []):
            new_nodes.update(tuple(p) for p in xy if inside_ring(*p, ring))
        new_nodes -= rolled
        history.append(
            {
                "iteration": iteration,
                "required_regions": plan["required_regions"],
                "unplanned_regions": plan["unplanned_regions"],
                "budget": budget,
                "proposed_rollback_regions": sorted(reject),
                "new_rollback_nodes": len(new_nodes),
                "fatal_constraints": bool(fatal),
                "measurement_conflicts": measurement_conflicts,
                "path_violations": quality.get("path_violations", 0),
                "max_path_violation_s": quality.get("max_path_violation_s", 0),
            }
        )
        if fatal and previous is not None:
            # 回退本身不能破坏可信路线或抹掉慢点。试算失败时恢复整份上一候选，
            # 仍因原修正不可验收而拒绝发布，不把产生冲突的试算当成模型结果。
            history[-1]["rollback_rejected"] = True
            fusion, targets, plan, budget, rolled = previous
            chk.rollback_xy = tuple(sorted(rolled))
            quality = fusion["quality"]
            quality["rollback_rejected"] = True
            new_nodes = set()
        if not new_nodes or fatal or iteration == 2:
            blocked = (
                fatal
                or not quality.get("ready", False)
                or quality.get("unsupported_holes", 0) > 0
                or not plan["plan_complete"]
                or not budget["sufficient"]
            )
            quality["preflight_blocked"] = bool(blocked)
            quality["preflight_budget_shortfall"] = budget["shortfall"]
            fusion["adoption"] = {
                "rounds": history,
                "rollback_nodes": len(rolled),
                "ready_for_validation": not blocked,
                "candidate_hash": candidate_hash(fusion),
                "targets_hash": targets_hash(targets),
                "policy": (
                    "预审控制独立补测；基础可靠的候选可作融合估计采用，"
                    "完整抽样状态单独记录，硬冲突仍回退"
                ),
            }
            return fusion
        previous = (deepcopy(fusion), targets, plan, budget, set(rolled))
        rolled.update(new_nodes)
        chk.rollback_xy = tuple(sorted(rolled))
        fusion = estimate_fusion(chk, samples)
    raise AssertionError("不可达的预审分支")
