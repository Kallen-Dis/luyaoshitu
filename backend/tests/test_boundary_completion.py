"""模型临界带补测、有限轮校正与独立验收的证据边界。"""

import asyncio
from copy import deepcopy
from unittest.mock import patch

import pytest
from test_fusion_completion import RouteNet
from test_gridcheck import POLY, C

from app.isochrone.fusion import apply_validation_gate, field_time
from app.isochrone.geometry import haversine_m, offset_point
from app.isochrone.gridcheck import (
    GridcheckConfig,
    _boundary_candidates,
    _Checker,
    _complete_fusion,
    _gap_candidates,
    _Lattice,
    _validate_fusion,
)


def surface(points):
    return {
        "geometry": {"type": "MultiPolygon", "coordinates": [[[[b, a] for a, b in POLY]]]},
        "time_field": {
            "vertices": [[C[1], C[0], 0], *[[lng, lat, t] for lat, lng, t in points]],
            "triangles": [[0, 1, 2]],
        },
        "unknown_inside_ray_km2": 0,
        "measured_points": [],
    }


def test_model_band_checks_new_positions_on_both_sides_and_recalculates_from_routes():
    a, b = offset_point(*C, 90, 400), offset_point(*C, 0, 400)

    class Net(RouteNet):
        calls = []

        async def walking_route(self, origin, destination, **kwargs):
            self.calls.append(destination)
            route = await super().walking_route(origin, destination, **kwargs)
            route["duration_s"] = 869 if haversine_m(*destination, *a) < 1 else 1035
            route["steps"][0]["duration_s"] = route["duration_s"]
            return route

    net = Net()
    chk = _Checker(net, C, POLY, None, GridcheckConfig(fusion_mode=True))
    lattice = _Lattice([{"lat": C[0], "lng": C[1]}], 100)
    fitted = []

    def estimate(*args):
        points = []
        for location, prior in ((a, 957), (b, 850)):
            observed = next(
                (p.route_s for p in chk.probes.values() if p.observed_location == location), None
            )
            points.append((*location, observed if observed is not None else prior))
        fitted.append(surface(points))
        return {"fusion": fitted[-1]}

    with patch("app.isochrone.gridcheck._estimate", side_effect=estimate):
        result = asyncio.run(_complete_fusion(chk, lattice, []))
    assert len(net.calls) == 2 and len(set(net.calls)) == 2
    assert result["correction_rounds"] <= 2
    assert {p["predicted_before_s"] for p in result["correction_points"]} == {850, 957}
    assert all(p["used_for_fit"] for p in result["correction_points"])
    assert field_time(fitted[-1]["time_field"], *a) == pytest.approx(869)
    assert field_time(fitted[-1]["time_field"], *b) == pytest.approx(1035)
    assert not _gap_candidates(list(chk.probes.values()), 900)  # 独立测点没有整格面积。


def test_model_band_does_not_spend_without_budget_or_reuse_validation_as_training():
    a, b = offset_point(*C, 90, 400), offset_point(*C, 0, 400)
    chk = _Checker(RouteNet(), C, POLY, None, GridcheckConfig(fusion_mode=True, max_routes=0))
    lattice = _Lattice([{"lat": C[0], "lng": C[1]}], 100)
    check = chk.add((800000, 1), 1, "validation", *a)
    check.measured, check.route_s = True, 869
    candidate = surface([(*a, 957), (*b, 850)])
    assert all(
        haversine_m(lat, lng, *a) >= 20 for lat, lng, _ in _boundary_candidates(chk, candidate)
    )
    with patch("app.isochrone.gridcheck._estimate", return_value={"fusion": candidate}):
        result = asyncio.run(_complete_fusion(chk, lattice, []))
    assert result["route_checks"] == 0 and result["correction_points"] == []
    assert list(chk.probes.values()) == [check] and check.kind == "validation"


def test_new_round_restores_previous_direct_counterexample_before_candidate_freeze():
    a = offset_point(*C, 90, 400)

    class Net(RouteNet):
        def recent_walking_targets(self, origin):
            return [a]

        def walking_route_cached(self, *args, **kwargs):
            return True

        async def walking_route(self, *args, **kwargs):
            route = await super().walking_route(*args, **kwargs)
            route["duration_s"] = 1052
            return route

    chk = _Checker(Net(), C, POLY, None, GridcheckConfig(fusion_mode=True, completion_rounds=0))
    chk.origin_preflight = {"passed": True}
    lattice = _Lattice([{"lat": C[0], "lng": C[1]}], 100)
    original = surface([(*a, 848), (*offset_point(*C, 0, 400), 900)])
    with patch("app.isochrone.gridcheck._estimate", return_value={"fusion": original}):
        result = asyncio.run(_complete_fusion(chk, lattice, []))
    assert result["indexed_routes_restored"] == 1
    point = next(iter(chk.probes.values()))
    assert point.route_s == 1052 and point.kind == "cache_evidence"
    assert point.route_cache_hit and chk.budget.routes_used == 0
    assert original["time_field"]["vertices"][1][2] == 848  # 上一冻结候选不被回写。


@pytest.mark.parametrize("path_s", [850, 1200])
def test_new_endpoint_on_a_slow_path_can_validate_but_known_reachable_path_cannot(path_s):
    a, b = offset_point(*C, 90, 400), offset_point(*C, 0, 400)
    chk = _Checker(RouteNet(), C, POLY, None, GridcheckConfig(fusion_mode=True))
    candidate = surface([(*a, 800), (*b, 800)])
    end = offset_point(*C, 90, 600)
    candidate["training_paths"] = [{"seconds": path_s, "path": [[C[1], C[0]], [end[1], end[0]]]}]
    original = deepcopy(candidate)
    selection = {
        "key": (800000, 0),
        "lat": a[0],
        "lng": a[1],
        "source": "changed_patch",
        "region": None,
        "region_nodes": [],
        "alternatives": [],
    }
    with patch(
        "app.isochrone.gridcheck.validation_plan",
        return_value=([selection], {"required_regions": 0, "required_sectors": 4}),
    ):
        result = asyncio.run(_validate_fusion(chk, None, [], {"fusion": candidate}, []))
    assert result["checked"] == (1 if path_s > 900 else 0)
    assert candidate == original  # 验收不反向校正已冻结的场。


def test_conflict_with_known_reachable_path_is_not_hidden_as_unknown_validation():
    candidate = {"target_s": 900, "quality": {"ready": True}}
    apply_validation_gate(
        candidate,
        {
            "requested": 4,
            "checked": 4,
            "coverage": {"complete": True},
            "points": [{"verdict": "unknown", "path_upper_conflict": True, "route_s": 1100}],
        },
    )
    assert not candidate["quality"]["ready"]
    assert candidate["quality"]["validation_significant_conflicts"] == 1


def test_shared_intersection_rounding_does_not_open_a_closed_contour():
    from app.isochrone.tin import merge_fragments, ring_area

    # 同一个共享端点落在 6 位小数取整分界两侧；旧实现留下两个不平衡端点。
    x = 50.0000005
    parts = [
        [(0, 0), (100, 0), (x - 1e-10, 100)],
        [(0, 0), (x + 1e-10, 100), (0, 100)],
    ]
    rings = merge_fragments(parts)
    assert len(rings) == 1
    assert sum(ring_area(r) for r in rings) == pytest.approx(7500, abs=0.0001)


def test_numerical_endpoint_dedup_keeps_real_separate_components():
    from app.isochrone.tin import merge_fragments

    a = [(0, 0), (1, 0), (1, 1), (0, 1)]
    b = [(1.001, 0), (2, 0), (2, 1), (1.001, 1)]
    assert len(merge_fragments([a, b])) == 2  # 1 毫米真实空档也不能拼接。


def test_thin_fragments_keep_directed_edge_multiplicity_and_close_without_gap_filling():
    from app.isochrone.tin import merge_fragments, ring_area

    # 三个无重叠裁片，其中一个是微米宽薄片。同向边贡献不能用 set 去重。
    fragments = [
        [
            [678.8303318803769, -51.92451461621601],
            [673.5851911223771, -50.0],
            [650.0, -50.0],
            [649.999999999886, -54.545452969185845],
        ],
        [
            [649.9999999997492, -60.000000000114255],
            [678.9149959556767, -54.21700080889169],
            [674.4089577072655, -52.326458390267454],
            [649.9999999998861, -54.545454545501755],
        ],
        [
            [674.4089577072655, -52.326458390267454],
            [678.8303318803769, -51.92451461621601],
            [649.999999999886, -54.545452969185845],
            [649.9999999998861, -54.545454545501755],
        ],
    ]
    rings = merge_fragments(fragments)
    assert len(rings) == 1
    assert len(rings[0]) == len(set(rings[0]))
    assert sum(ring_area(p) for p in rings) == pytest.approx(
        sum(ring_area(p) for p in fragments), abs=0.001
    )
