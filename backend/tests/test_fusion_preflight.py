"""固定起点、完整位置元数据、预审撤销与冻结验收的独立回归。零百度请求。"""

import asyncio
import math
from copy import deepcopy
from unittest.mock import patch

import pytest
from test_gridcheck import POLY, C, FakeNet, _rays, east_north
from test_localfield import RING
from test_path_field import value

from app.baidu.client import BaiduMapClient
from app.config import Settings
from app.isochrone.adoption import prepare_fusion
from app.isochrone.fusion import apply_fusion, apply_validation_gate, estimate_fusion
from app.isochrone.geometry import haversine_m, offset_point
from app.isochrone.gridcheck import GridcheckConfig, _Checker, _Lattice, _validate_fusion
from app.isochrone.localfield import Observation, RouteBound, fit_field
from app.isochrone.routequality import route_positions
from app.isochrone.tin import ring_area
from app.isochrone.validation import correction_regions, validation_request_budget


def route(start=C, end=None):
    end = end or offset_point(*C, 90, 300)
    return {
        "response_origin": {"lat": C[0], "lng": C[1]},
        "steps": [
            {
                "start_location": {"lat": start[0], "lng": start[1]},
                "end_location": {"lat": end[0], "lng": end[1]},
                "path": [start, end],
            }
        ],
    }


def test_response_echo_is_not_used_to_replace_a_different_path_origin():
    actual = offset_point(*C, 90, 40)
    result = route_positions(route(start=actual))
    assert result["start"] == actual
    assert result["response_origin"] == {"lat": C[0], "lng": C[1]}
    assert result["complete"] and result["position_basis"] == "step_and_path"


def test_step_position_conflict_is_not_hidden_by_a_plausible_polyline():
    data = route()
    data["steps"][0]["start_location"]["lat"] += 0.001
    assert not route_positions(data)["complete"]


def test_missing_step_geometry_is_not_accepted_as_a_continuous_route():
    data = route()
    data["steps"].append({"path": []})
    assert not route_positions(data)["complete"]


def test_legacy_path_cache_is_identified_without_inventing_metadata():
    data = route()
    del data["steps"][0]["start_location"]
    del data["steps"][0]["end_location"]
    assert route_positions(data)["position_basis"] == "path_only"


def test_different_origin_is_excluded_before_fit_instead_of_changing_the_anchor():
    net = FakeNet(pocket=False)
    chk = _Checker(net, C, POLY, None, GridcheckConfig(fusion_mode=True))
    p = chk.add((0, 24), 0, "cell", *offset_point(*C, 90, 300))
    asyncio.run(chk.verify_by_route(p))
    p.route_start = offset_point(*C, 180, 20)
    p.route_path[0] = p.route_start
    result = estimate_fusion(chk, _rays(net))
    assert result["origin_anchor"] == list(C)
    assert result["quality"]["excluded_origin_routes"] == 1
    assert result["quality"]["protected_routes"] == 0
    assert result["training_paths"] == []
    assert result["measured_points"][0]["time_s"] is None
    assert not result["quality"]["ready"]


def test_rollback_cannot_reclassify_a_reliable_slow_node_as_reachable():
    observation = Observation(450, 0, 1200, 80, True)
    old, _, _ = fit_field(RING, [], [observation])
    restored, faces, quality = fit_field(RING, [], [observation], rollback_xy=[(450, 0)])
    assert value(old, faces, 450, 0) > 900
    assert value(restored, faces, 450, 0) > 900
    assert quality["rollback_skipped_protected_vertices"] == 1 and quality["ready"]
    assert not quality["constraint_conflicts"]


def test_rollback_cannot_overwrite_a_known_reachable_endpoint():
    observation = Observation(650, 0, 800, 80, True)
    vertices, faces, quality = fit_field(RING, [], [observation], rollback_xy=[(650, 0)])
    assert value(vertices, faces, 650, 0) <= 800
    assert quality["rollback_skipped_protected_vertices"] == 1
    assert not quality["constraint_conflicts"] and quality["ready"]


def test_rollback_cannot_cut_through_a_reliable_fast_path():
    bound = RouteBound(((0, 0), (600, 0), (650, 0)), 800)
    vertices, faces, quality = fit_field(RING, [], [], routes=[bound], rollback_xy=[(650, 0)])
    assert all(value(vertices, faces, x, 0) <= 800 + 1e-5 for x in range(0, 651, 5))
    assert quality["rollback_skipped_protected_vertices"] == 1
    assert not quality["path_violations"] and quality["ready"]


def test_even_custom_borderline_reachable_time_keeps_an_endpoint_upper_bound():
    v, faces, quality = fit_field(RING, [], [Observation(650, 0, 190, 80, True)], target=200)
    assert value(v, faces, 650, 0) <= 190
    assert quality["ready"]


def test_preparation_rejects_using_any_validation_points_for_development():
    chk = _Checker(FakeNet(pocket=False), C, POLY, None, GridcheckConfig(fusion_mode=True))
    chk.add((800000, 1), 1, "validation", *C)
    with pytest.raises(ValueError, match="验收开始后"):
        prepare_fusion(chk, None, [], {}, [])


def test_preflight_cannot_erase_a_known_slow_response_to_make_a_clean_circle():
    chk = _Checker(FakeNet(pocket=False), C, POLY, None, GridcheckConfig(fusion_mode=True))
    lattice = _Lattice([{"lat": C[0], "lng": C[1]}], 100)
    candidate = estimate_fusion(chk, _rays(chk.client))
    lat, lng = offset_point(*C, 90, 300)
    candidate["measured_points"] = [
        {"lat": lat, "lng": lng, "time_s": 1200, "time_basis": "route_duration_includes_model_wait"}
    ]
    result = prepare_fusion(chk, lattice, [], candidate, _rays(chk.client))
    assert result["quality"]["measurement_conflicts"] == 1
    assert not result["adoption"]["ready_for_validation"]
    assert not chk.client.routes


def test_unplanned_sampling_does_not_erase_candidate_before_freeze():
    chk = _Checker(FakeNet(pocket=False), C, POLY, None, GridcheckConfig(fusion_mode=True))
    lattice = _Lattice([{"lat": C[0], "lng": C[1]}], 100)
    candidate = estimate_fusion(chk, _rays(chk.client))
    original = deepcopy(candidate)
    missed = {
        "required_regions": 1,
        "unplanned_regions": [0],
        "plan_complete": False,
        "regions": [{"id": 0, "node_indices": [0], "area_m2": 1}],
    }
    complete = {
        "required_regions": 0,
        "unplanned_regions": [],
        "plan_complete": True,
        "regions": [],
    }
    budget = {"shortfall": 0, "sufficient": True}
    rebuilt = deepcopy(candidate)
    with (
        patch("app.isochrone.adoption.validation_plan", side_effect=[([], missed), ([], complete)]),
        patch("app.isochrone.adoption.validation_request_budget", return_value=budget),
        patch("app.isochrone.adoption.estimate_fusion", return_value=rebuilt) as rebuild,
    ):
        result = prepare_fusion(chk, lattice, [], candidate, [])
    assert result["adoption"]["ready_for_validation"]
    assert result["quality"]["validation_plan_pending"]
    assert result["adoption"]["rollback_nodes"] == 0
    assert rebuild.call_count == 0
    assert candidate == original
    assert not chk.client.routes


@pytest.mark.parametrize("conflict", ["path", "measurement"])
def test_failed_rollback_restores_candidate_and_plan_but_never_publishes(conflict):
    chk = _Checker(FakeNet(pocket=False), C, POLY, None, GridcheckConfig(fusion_mode=True))
    candidate = estimate_fusion(chk, _rays(chk.client))
    x, y = candidate["time_field"]["vertices_xy"][0]
    candidate["quality"]["ready"] = False
    candidate["quality"]["unsupported_component_rings"] = [
        [(x - 1, y - 1), (x + 1, y - 1), (x + 1, y + 1), (x - 1, y + 1)]
    ]
    original = deepcopy(candidate)
    missed = {
        "required_regions": 1,
        "unplanned_regions": [0],
        "plan_complete": False,
        "regions": [{"id": 0, "node_indices": [0], "area_m2": 1}],
    }
    complete = {**missed, "unplanned_regions": [], "plan_complete": True}
    damaged = deepcopy(candidate)
    if conflict == "path":
        damaged["quality"]["path_violations"] = 1
    else:
        lat, lng = offset_point(*C, 90, 300)
        damaged["measured_points"] = [
            {
                "lat": lat,
                "lng": lng,
                "time_s": 1200,
                "time_basis": "route_duration_includes_model_wait",
            }
        ]
    with (
        patch("app.isochrone.adoption.validation_plan", side_effect=[([], missed), ([], complete)]),
        patch(
            "app.isochrone.adoption.validation_request_budget",
            return_value={"shortfall": 0, "sufficient": True},
        ),
        patch("app.isochrone.adoption.estimate_fusion", return_value=damaged),
    ):
        result = prepare_fusion(chk, None, [], candidate, [])
    assert result["time_field"] == original["time_field"]
    assert result["measured_points"] == original["measured_points"]
    assert result["adoption"]["rollback_nodes"] == 0 and chk.rollback_xy == ()
    assert result["quality"]["rollback_rejected"]
    assert result["quality"]["preflight_blocked"]
    assert not result["adoption"]["ready_for_validation"]
    assert candidate == original and not chk.client.routes


@pytest.mark.parametrize("offset", [0, 0.001, 2])
def test_actual_difference_contact_controls_regions_instead_of_mesh_face_adjacency(offset):
    a = [(0, 0), (1, 0), (0, 1)]
    b = [(1 + offset, 0), (1 + offset, 1), (offset, 1)]
    regions, _, _ = correction_regions([(0, 1, 2), (1, 3, 2)], [[a], [b]])
    assert len(regions) == (1 if offset == 0 else 2)


def test_two_separate_changes_inside_one_face_are_not_one_validation_region():
    regions, _, _ = correction_regions(
        [(0, 1, 2)], [[[(0, 0), (1, 0), (0, 1)], [(5, 5), (6, 5), (5, 6)]]]
    )
    assert len(regions) == 2


def test_corner_contact_does_not_count_as_connected_coverage():
    regions, _, _ = correction_regions(
        [(0, 1, 2), (1, 3, 2)], [[[(0, 0), (1, 0), (0, 1)]], [[(1, 0), (2, 0), (1, -1)]]]
    )
    assert len(regions) == 2


def test_degenerate_clip_contacts_are_not_validation_regions_but_real_tiny_areas_remain():
    tiny = [(10, 10), (10.1, 10), (10, 10.1)]  # 0.005 m²，仍需验证
    regions, _, parts = correction_regions(
        [(0, 1, 2)],
        [[[], [(0, 0)], [(0, 0), (1, 0), (2, 0)], tiny]],
    )
    assert len(regions) == 1 and parts == [[tiny]]


def test_changed_field_after_freeze_cannot_use_the_previous_validation_round():
    chk = _Checker(FakeNet(pocket=False), C, POLY, None, GridcheckConfig(fusion_mode=True))
    lattice = _Lattice([{"lat": C[0], "lng": C[1]}], 100)
    candidate = prepare_fusion(
        chk, lattice, [], estimate_fusion(chk, _rays(chk.client)), _rays(chk.client)
    )
    candidate["time_field"]["vertices"][0][2] += 1
    with pytest.raises(ValueError, match="冻结候选"):
        asyncio.run(_validate_fusion(chk, lattice, [], {"fusion": candidate}, _rays(chk.client)))
    assert not chk.client.routes


def test_preflight_rejection_keeps_frozen_targets_but_spends_no_new_routes():
    chk = _Checker(FakeNet(pocket=False), C, POLY, None, GridcheckConfig(fusion_mode=True))
    candidate = estimate_fusion(chk, _rays(chk.client))
    candidate["quality"]["preflight_blocked"] = True
    targets = [
        {
            "key": (800000, i),
            "lat": C[0],
            "lng": C[1] + i * 0.0001,
            "region": None,
            "source": "grid_offset",
            "region_nodes": [],
            "alternatives": [],
        }
        for i in range(4)
    ]
    with patch(
        "app.isochrone.gridcheck.validation_plan",
        return_value=(targets, {"required_regions": 0, "required_sectors": 4}),
    ):
        result = asyncio.run(_validate_fusion(chk, None, [], {"fusion": candidate}, []))
    assert result["checked"] == 0 and result["unknown"] == 4
    assert all("预审" in p["reason"] for p in result["points"])
    assert not chk.client.routes


def test_cached_validation_success_does_not_override_failed_preflight():
    candidate = {"quality": {"ready": True, "preflight_blocked": True}}
    apply_validation_gate(
        candidate,
        {"requested": 4, "checked": 4, "coverage": {"complete": True, "plan_complete": True}},
    )
    assert not candidate["quality"]["ready"]


def test_unverified_ten_minute_hole_is_hidden_without_substituting_a_different_field():
    chk = _Checker(FakeNet(pocket=False), C, POLY, None, GridcheckConfig(fusion_mode=True))
    p = chk.add((0, 24), 0, "cell", *offset_point(*C, 90, 300))
    asyncio.run(chk.verify_by_route(p))
    p.route_s = 700
    baseline = estimate_fusion(chk, _rays(chk.client))
    from app.isochrone.localfield import hole_evidence

    def uncertain_inner(rings, observations, target, *args, **kwargs):
        result = hole_evidence(rings, observations, target, *args, **kwargs)
        if target == 600:
            # 单独测试发布规则，不依赖某一版求解器恰好制造一个伪孔洞。
            result["unsupported_holes"] = 1
        return result

    with patch("app.isochrone.fusion.hole_evidence", side_effect=uncertain_inner):
        candidate = estimate_fusion(chk, _rays(chk.client))
    assert candidate["time_field"] == baseline["time_field"]
    assert candidate["quality"]["inner_topology"]["600"]["unsupported_holes"] > 0
    assert all(r["minutes"] != 10 for r in candidate["rings"])
    assert not candidate["quality"]["unsupported_holes"]


@pytest.mark.parametrize("budget", [0, 1])
def test_old_cache_position_upgrade_is_charged_and_obeys_budget(tmp_path, monkeypatch, budget):
    client = BaiduMapClient(Settings(server_ak="test", browser_ak="", cache_dir=tmp_path))
    destination = offset_point(*C, 90, 300)
    old = {
        "distance_m": 350,
        "duration_s": 400,
        "steps": [{"path": [C, destination], "distance_m": 350, "duration_s": 400}],
    }
    client._cache.write(client._walking_route_cache_path(C, destination, 10), old)
    calls = []

    async def request(endpoint, params, **kwargs):
        calls.append(endpoint)
        return {
            "result": {
                "routes": [
                    {
                        "distance": 350,
                        "duration": 400,
                        "steps": [
                            {
                                "distance": 350,
                                "duration": 400,
                                "start_location": {"lat": C[0], "lng": C[1]},
                                "end_location": {"lat": destination[0], "lng": destination[1]},
                                "path": f"{C[1]},{C[0]};{destination[1]},{destination[0]}",
                            }
                        ],
                    }
                ]
            }
        }

    monkeypatch.setattr(client, "_request", request)
    chk = _Checker(client, C, POLY, None, GridcheckConfig(fusion_mode=True, max_routes=budget))
    p = chk.add((0, 24), 0, "cell", *destination)
    # 收尾阶段先免费读过旧缓存，之后仍应能在预算内升级，不被 route_checked 锁死。
    asyncio.run(chk.verify_by_route(p, cache_only=True))
    assert p.route_position_basis == "path_only"
    forecast = validation_request_budget(chk, [{"lat": p.lat, "lng": p.lng}])
    assert forecast["cached_points"] == 0 and forecast["required_requests"] == 1
    asyncio.run(chk.verify_by_route(p))
    assert len(calls) == budget and chk.budget.routes_used == budget
    if budget:
        assert p.route_position_basis == "step_and_path" and not p.route_cache_hit
    else:
        assert p.route_checked and p.route_position_basis == "path_only"
        asyncio.run(chk.verify_by_route(p, cache_only=True))
        assert p.route_position_basis == "path_only" and p.route_cache_hit
        assert not calls
        result = estimate_fusion(chk, [])
        assert result["quality"]["protected_routes"] == 0
        assert result["quality"]["rejected_position_routes"] == 1


def test_independent_analytic_travel_field_can_publish_a_nontrivial_validated_correction():
    # 真值为独立解析公式；验收不能拿求解器自身预测充当真值。
    class AnalyticNet(FakeNet):
        async def walking_route(self, origin, destination, **kwargs):
            route = await super().walking_route(origin, destination, **kwargs)
            x, y = east_north(destination)
            seconds = 900 * haversine_m(*C, *destination) / 650 - 150 * max(
                0, 1 - math.hypot(x - 650, y) / 125
            )
            route["duration_s"] = seconds
            route["steps"][0]["duration_s"] = seconds
            return route

    async def run():
        net = AnalyticNet(pocket=False)
        chk = _Checker(net, C, POLY, None, GridcheckConfig(fusion_mode=True, max_routes=64))
        leaves = []
        for i, (x, y) in enumerate(((650, 0), (650, 60), (650, -60), (680, 0))):
            p = chk.add((700000, i), 0, "boundary_model", C[0] + y / 111320, C[1] + x / chk.mpl)
            await chk.verify_by_route(p)
            leaves.append(p)
        lattice = _Lattice([{"lat": C[0], "lng": C[1]}], 100)
        for y, x in ((-5, 0), (5, 0), (0, -5), (0, 5)):
            leaves.append(chk.add((y * 8, x * 8), 0, "cell", lattice=lattice))
        candidate = prepare_fusion(
            chk, lattice, leaves, estimate_fusion(chk, _rays(net)), _rays(net)
        )
        frozen = deepcopy(candidate["time_field"])
        validation = await _validate_fusion(chk, lattice, leaves, {"fusion": candidate}, _rays(net))
        assert candidate["time_field"] == frozen
        assert candidate["adoption"]["ready_for_validation"] and validation["coverage"]["complete"]
        assert validation["checked"] >= 4 and validation["disagreements"] == 0
        area = abs(ring_area([chk.xy(a, b) for a, b in POLY])) / 1e6
        feature = {
            "geometry": {"type": "Polygon", "coordinates": [[[b, a] for a, b in POLY]]},
            "properties": {"area_km2": area},
        }
        assert apply_fusion(
            feature, {"target_s": 900, "fusion": candidate, "validation": validation}
        )
        assert feature["properties"]["geometry_method"] == "fusion"
        assert feature["properties"]["area_km2"] > area + 0.001
        assert chk.budget.routes_used <= 64 and net.matrix_pairs == 0

    asyncio.run(run())
