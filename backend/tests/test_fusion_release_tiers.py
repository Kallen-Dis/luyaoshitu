from copy import deepcopy

import pytest
from test_gridcheck import POLY, C, FakeNet

from app.isochrone.fusion import apply_fusion, apply_validation_gate
from app.isochrone.gridcheck import GridcheckConfig, _Checker
from app.isochrone.validation import validation_request_budget


def candidate():
    return {
        "target_s": 900,
        "quality": {
            "ready": True,
            "solver_converged": True,
            "mesh_topology_stable": True,
            "origin_preflight": {"passed": True},
            "protected_routes": 8,
            "holes": 0,
        },
    }


def test_basic_valid_model_can_be_used_without_claiming_full_validation():
    f = candidate()
    f["quality"]["preflight_blocked"] = True  # 仅计划/预算未完整。
    apply_validation_gate(f, {"requested": 18, "checked": 0, "coverage": {"complete": False}})
    assert f["quality"]["ready"] and f["quality"]["sampling_pending"]
    assert f["quality"]["validation_status"] == "estimated"
    assert f["quality"]["validation_checked"] == 0
    again = deepcopy(f)
    apply_validation_gate(again, {"requested": 18, "checked": 0, "coverage": {"complete": False}})
    assert again["quality"]["ready"]  # 再次发布检查不把估计伪装成已核验。


@pytest.mark.parametrize(
    "key",
    [
        "measurement_conflicts",
        "clipped_paths",
        "domain_route_conflicts",
        "geographic_risk_unresolved",
        "path_violations",
        "unsupported_holes",
        "unsupported_components",
    ],
)
def test_hard_conflicts_block_both_estimated_and_verified_release(key):
    f = candidate()
    f["quality"][key] = 1
    apply_validation_gate(f, {"requested": 4, "checked": 4, "coverage": {"complete": True}})
    assert not f["quality"]["ready"] and f["quality"]["validation_status"] == "rejected"


def test_significant_holdout_conflict_and_failed_mesh_are_not_relaxed():
    f = candidate()
    apply_validation_gate(f, {"points": [{"verdict": "conflict", "route_s": 1100}]})
    assert not f["quality"]["ready"]
    f = candidate()
    f["quality"]["mesh_topology_stable"] = False
    apply_validation_gate(f, {})
    assert not f["quality"]["ready"]


def test_verified_label_requires_completed_sampling():
    f = candidate()
    apply_validation_gate(f, {"requested": 4, "checked": 4, "coverage": {"complete": True}})
    assert f["quality"]["validation_status"] == "verified" and not f["quality"]["sampling_pending"]


def test_estimated_tier_is_actually_adopted_as_main_geometry():
    geometry = {
        "type": "MultiPolygon",
        "coordinates": [[[[121, 31], [121.01, 31], [121.01, 31.01], [121, 31.01], [121, 31]]]],
    }
    f = candidate()
    f.update(geometry=geometry, area_km2=1, rings=[], pending_points=[])
    feature = {"geometry": geometry, "properties": {"area_km2": 1.1}}
    checked = {
        "fusion": f,
        "target_s": 900,
        "validation": {"checked": 0, "requested": 18, "coverage": {"complete": False}},
    }
    assert apply_fusion(feature, checked)
    assert feature["properties"]["geometry_method"] == "fusion"
    assert feature["properties"]["fusion"]["quality"]["validation_status"] == "estimated"
    assert feature["properties"]["area_km2"] == 1


def test_shared_backups_fit_eighteen_primary_requests_in_twenty_five():
    chk = _Checker(FakeNet(pocket=False), C, POLY, None, GridcheckConfig(max_routes=25))
    targets = [
        {"lat": C[0], "lng": C[1], "alternatives": [{"lat": C[0] + 0.001, "lng": C[1]}]}
        for _ in range(18)
    ]
    b = validation_request_budget(chk, targets)
    assert b["required_requests"] == 18 and b["backup_pool"] == 18
    assert b["planned_requests"] <= 25 and b["sufficient"] and b["shortfall"] == 0


def test_unavailable_water_service_allows_estimate_but_not_verified_label():
    f = candidate()
    f["quality"]["geographic_verification_pending"] = True
    apply_validation_gate(f, {"requested": 4, "checked": 4, "coverage": {"complete": True}})
    assert f["quality"]["ready"] and f["quality"]["validation_status"] == "estimated"


def test_budget_shortage_keeps_reliable_corrections_and_only_blocks_full_sampling(monkeypatch):
    from app.isochrone import adoption

    f = candidate()
    f.update(
        time_field={
            "vertices": [[121, 31, 800], [121.01, 31, 800], [121, 31.01, 800]],
            "triangles": [[0, 1, 2]],
        },
        measured_points=[
            {
                "lat": 31.001,
                "lng": 121.001,
                "time_s": 800,
                "time_basis": "route_duration_includes_model_wait",
            }
        ],
        geometry={"type": "MultiPolygon", "coordinates": []},
    )
    chk = _Checker(FakeNet(pocket=False), C, POLY, None, GridcheckConfig(max_routes=0))
    plan = {
        "unplanned_regions": [0],
        "required_regions": 1,
        "regions": [{"id": 0, "area_m2": 100, "node_indices": [0]}],
        "plan_complete": False,
    }
    monkeypatch.setattr(adoption, "validation_plan", lambda *args: ([], plan))
    monkeypatch.setattr(
        adoption, "validation_request_budget", lambda *args: {"shortfall": 1, "sufficient": False}
    )

    def forbid_refit(*args):
        raise AssertionError("Budget shortage must not erase reliable observations")

    monkeypatch.setattr(adoption, "estimate_fusion", forbid_refit)
    result = adoption.prepare_fusion(chk, None, [], f, [])
    assert result["time_field"] == f["time_field"]
    assert result["quality"]["measurement_conflicts"] == 0
    assert result["adoption"]["rollback_nodes"] == 0
    assert not result["quality"]["preflight_blocked"]
    assert result["quality"]["validation_plan_pending"]


@pytest.mark.parametrize("reported_local_count,adopted", [(1, True), (2, False)])
def test_publication_counts_local_estimated_holes_against_actual_geometry(
    reported_local_count, adopted
):
    geometry = {
        "type": "MultiPolygon",
        "coordinates": [
            [
                [[121, 31], [121.01, 31], [121.01, 31.01], [121, 31.01], [121, 31]],
                [
                    [121.001, 31.001],
                    [121.001, 31.00101],
                    [121.00101, 31.00101],
                    [121.00101, 31.001],
                    [121.001, 31.001],
                ],
            ]
        ],
    }
    f = candidate()
    f.update(geometry=geometry, area_km2=1, rings=[], pending_points=[])
    f["quality"].update(
        holes=1,
        supported_holes=0,
        unsupported_holes=0,
        local_hole_estimate_count=reported_local_count,
    )
    feature = {"geometry": geometry, "properties": {"area_km2": 1.1}}
    checked = {
        "fusion": f,
        "target_s": 900,
        "validation": {"checked": 0, "requested": 8, "coverage": {"complete": False}},
    }
    assert apply_fusion(feature, checked) is adopted
    if adopted:
        assert feature["properties"]["fusion"]["quality"]["validation_status"] == "estimated"
        assert len(feature["geometry"]["coordinates"][0]) == 2
