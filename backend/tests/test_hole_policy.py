"""内圈证据、线索方向、逐孔验收和起点早停；全部使用假客户端。"""

import asyncio
import math
from copy import deepcopy
from unittest.mock import patch

from test_gridcheck import POLY, C, FakeNet, _rays

from app.isochrone.adoption import candidate_hash, prepare_fusion
from app.isochrone.fusion import (
    apply_fusion,
    apply_validation_gate,
    estimate_fusion,
    rings_geometry,
)
from app.isochrone.geometry import offset_point
from app.isochrone.gridcheck import GridcheckConfig, _Checker
from app.isochrone.holepolicy import (
    associated_clues,
    shape_stable,
    smoothing_factor,
    topology_stable,
)
from app.isochrone.holevalidation import add_hole_targets, task_contains
from app.isochrone.localfield import Observation, contour_field, fit_field, hole_evidence
from app.isochrone.routequality import check_origin
from app.isochrone.validation import validation_coverage, validation_request_budget

HOLE = [(250, -50), (250, 50), (350, 50), (350, -50)]


def evidence(**kwargs):
    points = [Observation(275, 0, 1200, 80, True), Observation(325, 0, 1250, 80, True)]
    points += [Observation(x, y, 650, 80, True) for x, y in ((220, -80), (220, 80), (380, 80))]
    return hole_evidence([HOLE], points, 900, stable_rings=[HOLE], **kwargs)


def test_real_hole_needs_stability_not_closed_clue_wall():
    q = evidence(sensitivity_rings=[[HOLE], [HOLE]])
    assert q["supported_holes"] == 1
    assert q["hole_records"][0]["confirmed_clue_ids"] == []
    assert evidence(sensitivity_rings=[[HOLE], []])["unsupported_holes"] == 1


def test_same_points_inside_a_much_larger_ring_do_not_prove_stable_shape():
    huge = [(150, -150), (150, 150), (450, 150), (450, -150)]
    assert not shape_stable(HOLE, [huge])


def test_clue_requires_correct_side_and_only_softens_local_coupling():
    clue = {"id": 1, "near": (220, 0), "far": (320, 0), "status": "confirmed"}
    assert associated_clues(HOLE, [clue]) == [clue]
    assert not associated_clues(HOLE, [{**clue, "near": clue["far"], "far": clue["near"]}])
    assert smoothing_factor((250, 0), (300, 0), [clue]) == 0.25
    assert smoothing_factor((250, 0), (300, 0), [{**clue, "status": "unverified"}]) == 1
    assert smoothing_factor((250, 300), (300, 300), [clue]) == 1


def test_supported_hole_can_publish_only_after_its_independent_tasks_pass():
    outer = [(0, -300), (600, -300), (600, 300), (0, 300)]
    geom = rings_geometry([outer, HOLE], C)
    candidate = {
        "geometry": geom,
        "quality": {**evidence(), "ready": True},
        "pending_points": [],
        "area_km2": 0.35,
        "rings": [],
    }
    feature = {"geometry": rings_geometry([outer], C), "properties": {"area_km2": 0.36}}
    assert not apply_fusion(deepcopy(feature), {"fusion": deepcopy(candidate)})
    validation = {
        "requested": 6,
        "checked": 6,
        "points": [],
        "coverage": {"complete": True, "hole_validation_complete": True},
    }
    apply_validation_gate(candidate, validation)
    out = deepcopy(feature)
    assert apply_fusion(out, {"fusion": candidate, "target_s": 900, "validation": validation})
    assert len(out["geometry"]["coordinates"][0]) == 2


def test_unknown_or_conflicting_hole_target_never_counts_as_covered():
    plan = {"required_regions": 0, "required_sectors": 1, "hole_tasks": [{"id": "h"}]}
    for verdict in ("unknown", "conflict"):
        result = validation_coverage(plan, [{"verdict": verdict, "hole_task_id": "h", "sector": 0}])
        assert not result["complete"]


def test_hole_targets_require_actual_endpoint_in_the_same_role_and_direction():
    task = {"ring_xy": HOLE, "center_xy": (300, 0), "role": "inside", "sector": 0}
    assert task_contains(task, (300, 0))
    assert not task_contains(task, (370, 0))
    task.update(role="outside", sector=0)
    assert task_contains(task, (370, 20))
    assert not task_contains(task, (370, -20))


def test_hole_plan_has_independent_inside_and_four_outside_directions():
    chk = _Checker(FakeNet(pocket=False), C, POLY, None, GridcheckConfig(fusion_mode=True))
    fusion = estimate_fusion(chk, _rays(chk.client))
    fusion["quality"].update(evidence())
    targets = []
    tasks, missing = add_hole_targets(chk, fusion, targets)
    assert len(tasks) == len(targets) == 6 and not missing
    assert len({t["key"] for t in targets}) == 6
    assert all(task_contains(t["hole_task"], t["xy"]) for t in targets)


def test_preflight_allows_supported_hole_to_proceed_without_blanket_refusal():
    chk = _Checker(FakeNet(pocket=False), C, POLY, None, GridcheckConfig(fusion_mode=True))
    candidate = estimate_fusion(chk, _rays(chk.client))
    candidate["quality"].update(evidence())
    candidate["geometry"] = rings_geometry(
        [[(0, -300), (600, -300), (600, 300), (0, 300)], HOLE], C
    )
    plan = {"required_regions": 0, "unplanned_regions": [], "plan_complete": True, "regions": []}
    with (
        patch("app.isochrone.adoption.validation_plan", return_value=([], plan)),
        patch(
            "app.isochrone.adoption.validation_request_budget",
            return_value={"shortfall": 0, "sufficient": True},
        ),
    ):
        result = prepare_fusion(chk, None, [], candidate, [])
    assert result["adoption"]["ready_for_validation"]


def test_origin_preflight_does_not_move_center_to_snapped_road():
    class WrongStart(FakeNet):
        async def walking_route(self, origin, destination, **kwargs):
            data = await super().walking_route(origin, destination, **kwargs)
            start = offset_point(*origin, 90, 20)
            data["steps"][0]["path"][0] = start
            data["steps"][0]["start_location"] = {"lat": start[0], "lng": start[1]}
            return data

    chk = _Checker(WrongStart(pocket=False), C, POLY, None, GridcheckConfig(fusion_mode=True))
    points = [
        chk.add((0, i), 0, "cell", *offset_point(*C, bearing, 200))
        for i, bearing in enumerate((45, 135, 225, 315))
    ]
    result = asyncio.run(check_origin(chk, points))
    assert not result["passed"] and chk.center == C
    chk.origin_preflight = result
    before = len(chk.client.routes)
    p = chk.add((1, 0), 0, "cell", *offset_point(*C, 90, 400))
    asyncio.run(chk.verify_by_route(p))
    assert len(chk.client.routes) == before


def test_validation_budget_includes_frozen_backup_and_retry_reserve():
    chk = _Checker(FakeNet(pocket=False), C, POLY, None, GridcheckConfig(max_routes=2))
    targets = [{"lat": C[0], "lng": C[1], "alternatives": [{"lat": C[0] + 0.001, "lng": C[1]}]}]
    budget = validation_request_budget(chk, targets)
    assert budget["required_requests"] == 1 and budget["planned_requests"] == 2
    assert budget["backup_pool"] == 1 and budget["retry_reserve"] == 1
    assert budget["sufficient"]


def test_rollback_of_one_unsupported_hole_does_not_select_supported_hole_nodes():
    chk = _Checker(FakeNet(pocket=False), C, POLY, None, GridcheckConfig(fusion_mode=True))
    candidate = estimate_fusion(chk, _rays(chk.client))
    candidate["time_field"] = {
        "vertices_xy": [(1, 1), (2, 1), (1, 2), (11, 1), (12, 1), (11, 2)],
        "vertices": [[0, 0, 0]] * 6,
        "triangles": [[0, 1, 2], [3, 4, 5]],
    }
    candidate["quality"].update(
        unsupported_holes=1,
        hole_records=[
            {"supported": False, "ring_xy": [(0, 0), (0, 3), (3, 3), (3, 0)]},
            {"supported": True, "ring_xy": [(10, 0), (10, 3), (13, 3), (13, 0)]},
        ],
    )
    rebuilt = deepcopy(candidate)
    rebuilt["quality"].update(unsupported_holes=0, hole_records=[], ready=True)
    plan = {"required_regions": 0, "unplanned_regions": [], "plan_complete": True, "regions": []}
    with (
        patch("app.isochrone.adoption.validation_plan", return_value=([], plan)),
        patch(
            "app.isochrone.adoption.validation_request_budget",
            return_value={"shortfall": 0, "sufficient": True},
        ),
        patch("app.isochrone.adoption.estimate_fusion", return_value=rebuilt),
    ):
        prepare_fusion(chk, None, [], candidate, [])
    assert set(chk.rollback_xy) == {(1, 1), (2, 1), (1, 2)}


def test_dense_slow_patch_keeps_one_supported_hole_after_refinement():
    ring = [
        (600 * math.cos(i * math.tau / 36), 600 * math.sin(i * math.tau / 36)) for i in range(36)
    ]
    obs = [Observation(300 + x, y, 1200, 80, True) for x in (-40, 0, 40) for y in (-40, 0, 40)]
    obs += [
        Observation(
            300 + 120 * math.cos(i * math.tau / 8), 120 * math.sin(i * math.tau / 8), 650, 80, True
        )
        for i in range(8)
    ]
    v, t, _ = fit_field(ring, [], obs)
    original = contour_field(v, t, 900).rings
    v, t, _ = fit_field(ring, [], obs, refine=True)
    refined = contour_field(v, t, 900).rings
    q = hole_evidence(original, obs, 900, stable_rings=refined)
    assert q["holes"] == 1 and q["supported_holes"] == 1
    assert q["hole_evidence_points"] == [9] and q["hole_stable"][0]


def test_topology_check_rejects_new_disappearing_and_split_holes():
    small = [(500, 0), (500, 10), (510, 10), (510, 0)]
    assert topology_stable([HOLE], [HOLE])
    assert topology_stable([], [])
    assert not topology_stable([], [small])
    assert not topology_stable([small], [])
    assert not topology_stable([HOLE], [HOLE, small])
    assert not topology_stable([], None)
    # 两个原洞不能同时借用同一个相似的替代洞。
    assert not topology_stable([HOLE, HOLE], [HOLE, small])


def test_supported_original_hole_does_not_hide_an_extra_refinement_hole():
    small = [(500, 0), (500, 10), (510, 10), (510, 0)]
    assert evidence(sensitivity_rings=[[HOLE, small]])["unsupported_holes"] == 1


def test_complete_validation_cannot_override_failed_mesh_topology():
    outer = [(0, -300), (600, -300), (600, 300), (0, 300)]
    geometry = rings_geometry([outer], C)
    candidate = {
        "geometry": geometry,
        "quality": {"ready": True, "mesh_topology_stable": False},
        "target_s": 900,
    }
    feature = {"geometry": geometry, "properties": {"area_km2": 0.36}}
    validation = {"requested": 4, "checked": 4, "coverage": {"complete": True}, "points": []}
    assert not apply_fusion(feature, {"fusion": candidate, "validation": validation})
    assert feature["geometry"] == geometry
    assert "拓扑不稳定" in feature["properties"]["fusion_fallback"]


def test_freeze_protects_hole_evidence_and_geometry_as_well_as_field():
    source = {
        "time_field": {},
        "geometry": {"coordinates": []},
        "quality": {"hole_records": [{"supported": False}]},
    }
    frozen = candidate_hash(source)
    source["quality"]["hole_records"][0]["supported"] = True
    assert candidate_hash(source) != frozen
    source["quality"]["hole_records"][0]["supported"] = False
    source["quality"]["mesh_topology_stable"] = True
    assert candidate_hash(source) != frozen
    del source["quality"]["mesh_topology_stable"]
    source["geometry"]["coordinates"].append([1])
    assert candidate_hash(source) != frozen


def test_only_bounded_small_hole_changes_can_use_local_estimation():
    from app.isochrone.holepolicy import localized_topology_stable

    small = [(0, 0), (0, 10), (10, 10), (10, 0)]
    large = [(0, 0), (0, 50), (50, 50), (50, 0)]
    assert localized_topology_stable([small], [], 1000)
    assert localized_topology_stable([], [small], 1000)
    assert not localized_topology_stable([large], [], 1000)
    assert not localized_topology_stable([small], None, 1000)
    assert not localized_topology_stable([small], [], 50)
