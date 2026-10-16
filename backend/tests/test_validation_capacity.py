"""复杂修正片区的动态验收计划、缓存与嵌套预算。"""

import asyncio
import math
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_gridcheck import POLY, C, FakeNet, _rays

from app.isochrone.budget import FusionBudgetClient
from app.isochrone.fusion import apply_validation_gate
from app.isochrone.geometry import METERS_PER_DEG_LAT, meters_per_deg_lng
from app.isochrone.gridcheck import GridcheckConfig, _Checker, _Lattice, _validate_fusion
from app.isochrone.validation import (
    independence_space_blocked,
    interior_candidates,
    project_to_walk,
    select_spaced_targets,
    validation_plan,
    validation_request_budget,
)


def complex_plan(count, routes=80, blocked_region=None, side=10, paths=()):
    net = FakeNet(pocket=False)
    chk = _Checker(net, C, POLY, None, GridcheckConfig(fusion_mode=True, max_routes=routes))
    lattice = _Lattice([{"lat": C[0], "lng": C[1]}], 100)
    vertices, xy, faces = [], [], []
    for i in range(count):
        x, y = (i % 8 - 4) * 60 + 20, (i // 8 - 3) * 60 + 20
        k = len(vertices)
        for a, b in ((x, y), (x + side, y), (x, y + side)):
            xy.append([a, b])
            vertices.append(
                [C[1] + a / meters_per_deg_lng(C[0]), C[0] + b / METERS_PER_DEG_LAT, 1500]
            )
        faces.append([k, k + 1, k + 2])
    leaves = [
        chk.add((y * 8, x * 8), 0, "cell", lattice=lattice)
        for x, y in ((-5, 0), (5, 0), (0, -5), (0, 5))
    ]
    fusion = {
        "time_field": {"vertices": vertices, "vertices_xy": xy, "triangles": faces},
        "measured_points": [],
        "training_paths": list(paths),
    }
    if blocked_region is not None:
        points = vertices[blocked_region * 3 : blocked_region * 3 + 3]
        fusion["measured_points"] = [
            {
                "lng": sum(p[0] for p in points) / 3,
                "lat": sum(p[1] for p in points) / 3,
                "time_s": 1500,
            }
        ]
    targets, plan = validation_plan(chk, lattice, leaves, fusion, _rays(net))
    return chk, net, targets, plan


@pytest.mark.parametrize("regions", [39, 56])
def test_plan_can_cover_every_region_above_the_old_32_point_limit(regions):
    chk, net, targets, plan = complex_plan(regions, routes=200)  # 主目标、冻结备选和重试。
    assert plan["required_regions"] == regions
    assert len(targets) == plan["planned_points"] == plan["target_points"] == regions + 4
    assert {q["region"] for q in targets if q["region"] is not None} == set(range(regions))
    assert {q["sector"] for q in targets} == set(range(4))
    assert plan["plan_complete"] and plan["unplanned_regions"] == []
    budget = validation_request_budget(chk, targets)
    assert budget["sufficient"] and budget["required_requests"] == regions + 4
    assert not net.routes and net.matrix_pairs == 0


def test_small_budget_does_not_shrink_the_plan_or_relax_region_requirements():
    chk, net, targets, plan = complex_plan(56, routes=40)
    budget = validation_request_budget(chk, targets)
    assert len(targets) == 60 and plan["required_regions"] == 56
    assert budget["available_requests"] == 40
    assert budget["shortfall"] == budget["planned_requests"] - 40
    assert budget["planned_requests"] >= 60  # 主目标之外还预留备选和重试。
    assert not budget["sufficient"]
    assert not net.routes and net.matrix_pairs == 0


def test_budget_deducts_outer_retry_attempts_and_preserves_free_cache():
    class Net(FakeNet):
        def walking_route_cached(self, origin, destination, **kwargs):
            return destination[0] < C[0] + 0.00015

    net = Net(pocket=False)
    outer = FusionBudgetClient(net, 0, 7)
    outer.route_attempts = 6
    inner = FusionBudgetClient(outer, 0, 100)
    chk = _Checker(inner, C, POLY, None, GridcheckConfig(fusion_mode=True, max_routes=100))
    targets = [{"lat": C[0] + i * 0.0001, "lng": C[1]} for i in range(5)]
    budget = validation_request_budget(chk, targets)
    assert budget["cached_points"] == 2 and budget["required_requests"] == 3
    assert budget["available_requests"] == 1 and budget["shortfall"] == 2
    assert outer.route_attempts == 6 and not net.routes


def test_unplanned_critical_region_still_rejects_an_inconsistent_coverage_claim():
    estimate = {"quality": {"ready": True}}
    apply_validation_gate(
        estimate,
        {
            "requested": 60,
            "checked": 60,
            "coverage": {"complete": True, "plan_complete": False, "unplanned_regions": [55]},
        },
    )
    assert not estimate["quality"]["ready"]
    assert estimate["quality"]["validation_unplanned_regions"] == 1


def test_cached_goals_need_no_additional_budget():
    client = SimpleNamespace(walking_route_cached=lambda *a, **k: True)
    chk = _Checker(client, C, POLY, None, GridcheckConfig(fusion_mode=True, max_routes=0))
    budget = validation_request_budget(chk, [{"lat": C[0], "lng": C[1]}])
    assert budget["sufficient"] and budget["required_requests"] == 0


def test_region_without_independent_target_is_reported_instead_of_dropped():
    _, _, _, plan = complex_plan(56, blocked_region=20)
    assert plan["required_regions"] == 56 and plan["target_points"] == 60
    assert plan["unplanned_regions"] == [20]
    assert plan["planned_regions"] == 55 and not plan["plan_complete"]
    assert plan["unplanned_details"][0]["rejected_training"] > 0
    assert plan["unplanned_details"][0]["eligible_candidates"] == 0


def test_independent_interior_position_is_found_when_centroid_is_a_training_point():
    chk, net, targets, plan = complex_plan(1, blocked_region=0, side=40)
    assert plan["planned_regions"] == 1 and not plan["unplanned_regions"]
    center = (-220 + 40 / 3, -160 + 40 / 3)
    q = next(q for q in targets if q["region"] == 0)
    assert math.dist(chk.xy(q["lat"], q["lng"]), center) >= 10
    assert not net.routes and net.matrix_pairs == 0


def test_interior_search_does_not_leave_thin_convex_patch():
    from app.isochrone.tin import inside_ring

    patch = [(0, 0), (100, 0), (0, 0.001)]
    assert all(inside_ring(x, y, patch) for x, y in interior_candidates(patch))


def candidate(region, i, x):
    return {
        "key": (800000 + region, i),
        "xy": (x, 0),
        "region": region,
        "sector": 0,
        "source": "changed_patch",
        "threshold_distance": 0,
    }


def test_scarce_region_keeps_its_only_target_instead_of_being_squeezed_out():
    pool = [candidate(0, 0, 0), candidate(0, 1, 30), candidate(1, 0, 5)]
    selected, regions, _ = select_spaced_targets(pool, 2)
    assert regions == {0, 1}
    assert {q["xy"][0] for q in selected} == {5, 30}
    assert select_spaced_targets(list(reversed(pool)), 2) == (selected, regions, {0})


def test_equal_size_pools_choose_position_that_preserves_other_regions():
    pool = [
        candidate(r, i, x)
        for r, xs in enumerate(((0, 30), (5, 6), (60, 61)))
        for i, x in enumerate(xs)
    ]
    selected, regions, _ = select_spaced_targets(pool, 3)
    assert regions == {0, 1, 2}
    assert selected[0]["xy"] == (30, 0)


def test_incompatible_regions_do_not_count_the_same_location_twice():
    selected, regions, _ = select_spaced_targets([candidate(0, 0, 0), candidate(1, 0, 9)], 2)
    assert len(selected) == len(regions) == 1


def test_exclusion_proof_does_not_confuse_failed_search_with_impossibility():
    assert independence_space_blocked([[(0, 0), (12, 0), (0, 12)]], [(0, 0), (12, 0), (0, 12)], [])
    assert not independence_space_blocked([[(0, 0), (30, 0), (0, 30)]], [(0, 0)], [])
    assert independence_space_blocked([[(0, 0), (40, 0), (40, 4), (0, 4)]], [], [((0, 2), (40, 2))])
    assert not independence_space_blocked([], [], [])


def test_nearest_outside_road_does_not_hide_an_eligible_road_inside_patch():
    part = [(0, 0), (40, 0), (0, 40)]
    roads = [((-5, -1), (45, -1)), ((0, 8), (30, 8))]
    assert project_to_walk(10, 2, roads, [], part=part) == (10, 8)


@pytest.mark.parametrize("seconds, offset", [(1200, 8), (500, 5), (1200, -30)])
def test_path_candidates_require_new_time_and_stay_inside_changed_patch(seconds, offset):
    mpl = meters_per_deg_lng(C[0])
    route = {
        "seconds": seconds,
        "path": [
            [C[1] + x / mpl, C[0] + (-160 + offset) / METERS_PER_DEG_LAT] for x in (-220, -180)
        ],
    }
    chk, net, targets, _ = complex_plan(1, side=40, paths=[route])
    road_targets = [q for q in targets if q["source"] == "walking_path"]
    if seconds == 1200 and offset == 8:
        assert road_targets
        assert all(abs(chk.xy(q["lat"], q["lng"])[1] + 152) < 1e-6 for q in road_targets)
    else:
        assert not road_targets
    assert not net.routes and net.matrix_pairs == 0


def test_large_budget_blocked_plan_does_not_scan_field_or_send_routes():
    net = FakeNet(pocket=False)
    chk = _Checker(net, C, POLY, None, GridcheckConfig(fusion_mode=True, max_routes=0))
    selections = [
        {
            "key": (800000, i),
            "lat": C[0] + 0.001,
            "lng": C[1] + i * 0.00001,
            "source": "grid_offset",
            "region": None,
            "region_nodes": [],
            "alternatives": [],
        }
        for i in range(100)
    ]
    surface = {
        "geometry": {"type": "Polygon", "coordinates": [[[b, a] for a, b in POLY]]},
        "time_field": {},
        "measured_points": [],
    }
    with (
        patch(
            "app.isochrone.gridcheck.validation_plan",
            return_value=(selections, {"required_regions": 0, "required_sectors": 4}),
        ),
        patch(
            "app.isochrone.gridcheck.field_time",
            side_effect=AssertionError("未取到路线的点不扫描场"),
        ),
    ):
        validation = asyncio.run(_validate_fusion(chk, None, [], {"fusion": surface}, []))
    assert validation["requested"] == validation["unknown"] == 100
    assert all(p["reason"] == "本地路线预算不足" for p in validation["points"])
    assert not net.routes and not net.matrix_pairs
