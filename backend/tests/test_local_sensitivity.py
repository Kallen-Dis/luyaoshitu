"""局部敏感性按物理范围和原始证据判断，不扩大正式几何或填掉慢点。"""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from shapely.geometry import Polygon, box

from app.isochrone.adoption import candidate_hash
from app.isochrone.domain import GeographicDomain
from app.isochrone.holepolicy import local_sensitivity
from app.isochrone.localfield import Observation, RouteBound


def ring(x, radius):
    return [
        (x - radius, -radius),
        (x - radius, radius),
        (x + radius, radius),
        (x + radius, -radius),
    ]


def case(**kwargs):
    options = dict(
        rings=[ring(0, 6)],
        variants=[[ring(0, 3)], [ring(0, 7), ring(200, 13)]],
        observations=[
            Observation(0, 0, 944, protected=True),
            Observation(200, 0, 1029, protected=True),
        ],
        routes=[],
        target=900,
        area_limit=1000,
    )
    options.update(kwargs)
    return local_sensitivity(**options)


def test_small_hole_and_new_local_hole_at_other_slow_point_can_be_estimated():
    audit = case()
    assert audit["passed"] and len(audit["regions"]) == 2
    assert audit["changed_area_m2"] == pytest.approx(196 + 676)
    assert not audit["rollback_rings"]


@pytest.mark.parametrize("variants", [[None], [[ring(0, 40)]], [[ring(500, 13)]]])
def test_failed_solve_large_expansion_or_unanchored_new_hole_cannot_pass(variants):
    assert not case(variants=variants)["passed"]


def test_unanchored_local_change_proposes_local_rollback_only():
    audit = case(observations=[Observation(0, 0, 944, protected=True)])
    assert not audit["passed"]
    assert len(audit["rollback_rings"]) == 1
    assert Polygon(audit["rollback_rings"][0]).covers(Polygon(ring(200, 13)))
    assert not Polygon(audit["rollback_rings"][0]).intersects(Polygon(ring(0, 6)))


@pytest.mark.parametrize("conflict", ["fast", "path", "water", "budget"])
def test_local_estimation_keeps_hard_evidence_and_total_area_checks(conflict):
    options = {}
    if conflict == "fast":
        options["observations"] = [
            Observation(0, 0, 944, protected=True),
            Observation(2, 0, 850, protected=True),
        ]
    elif conflict == "path":
        options["routes"] = [RouteBound(((-30, 0), (30, 0)), 850)]
    elif conflict == "water":
        domain = GeographicDomain((0, 0), Polygon(), box(-1, -1, 1, 1), "test", "1")
        domain.xy_shape = lambda _: box(-2, -2, 2, 2)
        options["domain"] = domain
    else:
        options["area_limit"] = 100
    assert not case(**options)["passed"]


def test_unchanged_large_hole_is_still_allowed_but_moved_large_hole_is_not():
    assert case(rings=[ring(0, 100)], variants=[[ring(0, 100)]])["passed"]
    assert not case(rings=[ring(0, 100)], variants=[[ring(50, 100)]])["passed"]


def test_local_sensitivity_is_frozen_with_candidate():
    candidate = {"quality": {"local_sensitivity": case()}}
    changed = deepcopy(candidate)
    changed["quality"]["local_sensitivity"]["regions"][0]["area_m2"] += 1
    assert candidate_hash(changed) != candidate_hash(candidate)


def test_local_slow_cap_preserves_measured_node_without_spreading_to_other_slow_points():
    from app.isochrone.localfield import correction_support

    observations = [
        Observation(0, 0, 1052, protected=True),
        Observation(200, 0, 1100, protected=True),
    ]
    vertices = [SimpleNamespace(x=x, y=0) for x in (0, 40, 200, 240)]
    normal = correction_support(vertices, observations)
    local = correction_support(vertices, observations, correction_caps=[(0, 0, 25)])
    assert local[0] == normal[0] == 1
    assert local[1] == 0 < normal[1]
    assert local[2:] == normal[2:]


def test_local_retry_preserves_actual_measured_node_and_other_regions(monkeypatch):
    from app.isochrone import adoption

    chk = SimpleNamespace(
        cfg=SimpleNamespace(target_s=900), probes={}, xy=lambda lat, lng: (lng, lat)
    )
    candidate = {
        "quality": {
            "ready": False,
            "mesh_topology_stable": False,
            "local_sensitivity": {"rollback_rings": [[(0, 0), (0, 3), (3, 3), (3, 0)]]},
        },
        "time_field": {
            "vertices_xy": [(1, 1), (2, 1), (1, 2), (10, 10)],
            "vertices": [[1, 1, 800], [2, 1, 800], [1, 2, 800], [10, 10, 800]],
            "triangles": [[0, 1, 2]],
        },
        "measured_points": [
            {"lat": 1, "lng": 1, "time_s": 800, "time_basis": "route_duration_includes_model_wait"}
        ],
    }
    rebuilt = deepcopy(candidate)
    rebuilt["quality"].update(ready=True, mesh_topology_stable=True, local_sensitivity={})
    plan = {"required_regions": 0, "unplanned_regions": [], "plan_complete": True, "regions": []}
    monkeypatch.setattr(adoption, "validation_plan", lambda *args: ([], plan))
    monkeypatch.setattr(
        adoption, "validation_request_budget", lambda *args: {"shortfall": 0, "sufficient": True}
    )
    monkeypatch.setattr(adoption, "estimate_fusion", lambda *args: rebuilt)
    result = adoption.prepare_fusion(chk, None, [], candidate, [])
    assert set(chk.rollback_xy) == {(2, 1), (1, 2)}
    assert result["quality"]["ready"] and result["quality"]["measurement_conflicts"] == 0
