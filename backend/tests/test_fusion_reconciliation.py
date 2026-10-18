"""局部纠错不是豁免反例：保存失败轮次、冻结新候选、要求新的独立证据。"""

import asyncio
from copy import deepcopy

import pytest
from test_gridcheck import POLY, C, FakeNet

from app.isochrone.fusion import apply_validation_gate
from app.isochrone.gridcheck import GridcheckConfig, _Checker
from app.isochrone.reconciliation import reconcile_fusion, repair_scope, unresolved_evidence


def candidate(seconds):
    return {
        "target_s": 900,
        "geometry": {"type": "MultiPolygon", "coordinates": [[[[b, a] for a, b in POLY]]]},
        "time_field": {
            "vertices": [
                [C[1] - 0.01, C[0] - 0.01, seconds],
                [C[1] + 0.01, C[0] - 0.01, seconds],
                [C[1], C[0] + 0.01, seconds],
            ],
            "triangles": [[0, 1, 2]],
        },
        "quality": {
            "ready": True,
            "solver_converged": True,
            "mesh_topology_stable": True,
            "origin_preflight": {"passed": True},
            "protected_routes": 8,
        },
    }


@pytest.mark.parametrize("seconds,observed", [(910, 800), (850, 1014)])
def test_no_boundary_tolerance_hides_old_false_inclusion_or_new_false_exclusion(seconds, observed):
    records = [{"actual_endpoint": [C[1], C[0]], "route_s": observed}]
    assert len(unresolved_evidence(candidate(seconds), records)) == 1


def test_corrected_candidate_uses_new_round_and_keeps_original_failure(monkeypatch):
    from app.isochrone import gridcheck, reconciliation

    chk = _Checker(FakeNet(), C, POLY, None, GridcheckConfig(fusion_mode=True))
    original = candidate(1000)
    promoted = chk.add((800000, 0), 1, "validation", *C)
    calls = []

    async def validate(chk, lattice, leaves, estimate, samples):
        calls.append(deepcopy(estimate["fusion"]))
        if len(calls) == 1:
            return {
                "requested": 4,
                "checked": 1,
                "points": [
                    {
                        "probe_key": [800000, 0],
                        "verdict": "conflict",
                        "significant_conflict": True,
                        "actual_endpoint": [C[1], C[0]],
                        "route_s": 800,
                        "sector": 0,
                    }
                ],
                "coverage": {"complete": False},
            }
        assert promoted.kind == "repair_evidence"
        assert chk.validation_round == 1 and chk.retired_validation_xy == [(0, 0)]
        return {
            "requested": 4,
            "checked": 2,
            "points": [
                {"verdict": "consistent", "route_s": 750, "sector": 1},
                {"verdict": "consistent", "route_s": 800, "sector": 2},
            ],
            "coverage": {"complete": False},
        }

    monkeypatch.setattr(
        reconciliation, "prepare_fusion", lambda chk, lattice, leaves, f, s: deepcopy(f)
    )
    monkeypatch.setattr(gridcheck, "_validate_fusion", validate)
    monkeypatch.setattr(reconciliation, "estimate_fusion", lambda *args: candidate(800))
    result, validation = asyncio.run(reconcile_fusion(chk, None, [], original, []))
    assert result["quality"]["ready"] and result["quality"]["validation_status"] == "estimated"
    assert len(result["repair"]["rounds"]) == 2
    first = result["repair"]["rounds"][0]
    assert first["validation"]["points"][0]["verdict"] == "conflict"
    assert first["candidate_hash"] != result["repair"]["rounds"][1]["candidate_hash"]
    assert validation["checked"] == 2  # 旧轮的一个反例没有混入新点通过数。
    assert original["quality"]["ready"] and original["time_field"]["vertices"][0][2] == 1000


@pytest.mark.parametrize(
    "flag", ["repair_unresolved_conflicts", "repair_scope_exceeded", "repair_verification_pending"]
)
def test_incomplete_or_unbounded_repair_cannot_be_published(flag):
    f = candidate(800)
    f["quality"][flag] = True
    apply_validation_gate(f, {"requested": 4, "checked": 4, "coverage": {"complete": True}})
    assert not f["quality"]["ready"]


def test_invalid_repair_geometry_is_rejected_instead_of_crashing_analysis():
    chk = _Checker(FakeNet(), C, POLY, None, GridcheckConfig(fusion_mode=True))
    before = candidate(800)
    after = deepcopy(before)
    # 自交的蝴蝶结轮廓不能进入集合差运算，也不能静默 make_valid 后发布。
    x, y = C[1], C[0]
    after["geometry"] = {
        "type": "Polygon",
        "coordinates": [[[x, y], [x + 0.001, y + 0.001], [x, y + 0.001], [x + 0.001, y], [x, y]]],
    }
    result = repair_scope(before, after, [], chk)
    assert result["passed"] is False and result["reason"] == "invalid_geometry"


def test_scope_is_checked_after_preparation_and_before_new_requests(monkeypatch):
    from app.isochrone import gridcheck, reconciliation

    chk = _Checker(FakeNet(), C, POLY, None, GridcheckConfig(fusion_mode=True))
    chk.add((800000, 0), 1, "validation", *C)
    calls = []

    async def validate(*args):
        calls.append(1)
        assert len(calls) == 1  # 新候选的预检扩大了局部修正，不能再花请求验收。
        return {
            "checked": 1,
            "requested": 1,
            "coverage": {"complete": False},
            "points": [
                {
                    "probe_key": [800000, 0],
                    "verdict": "conflict",
                    "significant_conflict": True,
                    "actual_endpoint": [C[1], C[0]],
                    "route_s": 800,
                    "sector": 0,
                }
            ],
        }

    def prepare(chk, lattice, leaves, f, samples):
        result = deepcopy(f)
        if chk.validation_round:
            for face in result["geometry"]["coordinates"]:
                for ring in face:
                    for pos in ring:
                        pos[0] += 0.01
        return result

    monkeypatch.setattr(gridcheck, "_validate_fusion", validate)
    monkeypatch.setattr(reconciliation, "prepare_fusion", prepare)
    monkeypatch.setattr(reconciliation, "estimate_fusion", lambda *args: candidate(800))
    result, validation = asyncio.run(reconcile_fusion(chk, None, [], candidate(1000), []))
    assert not result["quality"]["ready"] and result["quality"]["repair_scope_exceeded"]
    assert result["repair"]["rounds"][0]["repair_scope"]["passed"] is False
    assert validation["checked"] == 0 and validation["points"] == []


def test_capping_one_hole_does_not_consume_the_two_local_rollback_rounds(monkeypatch):
    from app.isochrone import adoption

    chk = _Checker(FakeNet(), C, POLY, None, GridcheckConfig(fusion_mode=True))
    initial = candidate(1200)
    initial["time_field"]["vertices_xy"] = [
        chk.xy(v[1], v[0]) for v in initial["time_field"]["vertices"]
    ]
    x, y = initial["time_field"]["vertices_xy"][0]
    tiny = [[x - 1, y - 1], [x + 1, y - 1], [x + 1, y + 1], [x - 1, y + 1]]
    initial["quality"]["hole_records"] = [
        {
            "supported": False,
            "slow_points": 1,
            "area_m2": 4,
            "ring_xy": tiny,
        }
    ]
    initial["measured_points"] = [
        {
            "lng": initial["time_field"]["vertices"][0][0],
            "lat": initial["time_field"]["vertices"][0][1],
            "time_s": 1200,
            "time_basis": "route_duration_includes_model_wait",
        }
    ]
    stages = []
    for index in (1, 2, None):
        stage = deepcopy(initial)
        stage["quality"]["hole_records"] = []
        if index is not None:
            x, y = stage["time_field"]["vertices_xy"][index]
            stage["quality"]["unsupported_component_rings"] = [
                [
                    [x - 1, y - 1],
                    [x + 1, y - 1],
                    [x + 1, y + 1],
                    [x - 1, y + 1],
                ]
            ]
        stages.append(stage)
    monkeypatch.setattr(
        adoption,
        "validation_plan",
        lambda *args: (
            [],
            {
                "regions": [],
                "required_regions": 0,
                "unplanned_regions": [],
                "plan_complete": True,
            },
        ),
    )
    monkeypatch.setattr(
        adoption,
        "validation_request_budget",
        lambda *args: {
            "shortfall": 0,
            "sufficient": True,
        },
    )
    rebuilt = []

    def estimate(*args):
        rebuilt.append(1)
        return stages[len(rebuilt) - 1]

    monkeypatch.setattr(adoption, "estimate_fusion", estimate)
    result = adoption.prepare_fusion(chk, None, [], initial, [])
    assert len(rebuilt) == 3
    assert result["adoption"]["cap_rounds"] == 1
    assert result["adoption"]["rollback_rounds"] == 2
    assert result["adoption"]["rollback_nodes"] == 2
    assert result["adoption"]["ready_for_validation"]
