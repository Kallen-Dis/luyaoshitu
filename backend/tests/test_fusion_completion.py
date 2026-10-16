"""新地点收尾核验、独立路线口径、实际终点与失败保护。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_gridcheck import POLY, C, FakeNet, _blindspots, _run

from app.isochrone.budget import FusionBudgetClient
from app.isochrone.fusion import estimate_fusion, fusion_time, sync_heat_measurements
from app.isochrone.geometry import haversine_m, offset_point
from app.isochrone.gridcheck import GridcheckConfig, _Checker, _complete_fusion, _Lattice


class RouteNet:
    def __init__(self, end_offset=0, start_offset=0):
        self.end_offset = end_offset
        self.start_offset = start_offset

    async def walking_route(self, origin, destination, **kwargs):
        end = offset_point(*destination, 0, self.end_offset)
        start = offset_point(*origin, 0, self.start_offset)
        distance = haversine_m(*start, *end) * 1.2
        return {
            "distance_m": distance,
            "duration_s": 700,
            "steps": [
                {
                    "distance_m": distance,
                    "duration_s": 700,
                    "path": [start, end],
                    "start_location": {"lat": start[0], "lng": start[1]},
                    "end_location": {"lat": end[0], "lng": end[1]},
                }
            ],
        }


def checked_probe(net, raw_s=1000):
    chk = _Checker(net, C, POLY, None, GridcheckConfig(fusion_mode=True))
    lat, lng = offset_point(*C, 90, 500)
    p = chk.add((0, 40), 0, "cell", lat, lng)
    p.measured = True
    p.raw_s = raw_s
    p.dist_m = raw_s * 1.17 if raw_s is not None else None
    asyncio.run(chk.verify_by_route(p))
    return chk, p


def test_checked_route_time_is_not_combined_with_another_interfaces_distance():
    chk, p = checked_probe(RouteNet())
    assert p.status(900) == "reach"
    assert fusion_time(p, 900) == 700
    assert p.route_m != p.dist_m


def test_own_route_can_recover_a_failed_matrix_measurement():
    chk, p = checked_probe(RouteNet(), None)
    assert p.route_authoritative
    assert fusion_time(p, 900) == 700


def test_valid_matrix_with_model_wait_is_a_time_estimate_on_both_sides():
    for raw, delay in ((700, 100), (850, 100)):
        p = SimpleNamespace(
            off_network=False, blocked=False, route_authoritative=False, eff_s=raw + delay
        )
        assert fusion_time(p, 900) == raw + delay


@pytest.mark.parametrize("offset", [30, 100])
def test_shifted_route_observation_is_located_at_its_actual_endpoint(offset):
    chk, p = checked_probe(RouteNet(end_offset=offset))
    assert p.route_authoritative == (offset <= 50)
    assert p.observed_location is not None
    if offset > 50:
        assert fusion_time(p, 900) is None
    feature = estimate_fusion(chk, [])
    measurement = feature["measured_points"][0]
    assert measurement["source"] == "relocated_route"
    assert measurement["lat"] == pytest.approx(p.observed_location[0], abs=1e-6)
    assert measurement["requested_lat"] == pytest.approx(p.lat, abs=1e-6)
    assert not feature["pending_points"]


@pytest.mark.parametrize("net", [RouteNet(end_offset=300), RouteNet(start_offset=200)])
def test_unreliable_route_endpoints_still_remain_unknown(net):
    chk, p = checked_probe(net)
    assert not p.route_authoritative and p.observed_location is None
    assert fusion_time(p, 900) is None


def test_a_new_place_completes_its_estimate_within_the_shared_hard_budget():
    net = FakeNet(pocket=False)
    budget = FusionBudgetClient(net, 400, 40)
    checked = _run(budget, fusion_mode=True)
    assert checked["fusion"]["unknown_inside_ray_km2"] == 0
    assert checked["completion"]["route_verified_points"] > 0
    assert budget.routes_used <= 40 and budget.pairs_used <= 400
    assert checked["validation"]["requested"] <= checked["validation"]["coverage"]["target_points"]


def test_failed_seed_is_recorded_without_excavating_a_supported_interior():
    net = FakeNet(pocket=False)
    blind = _blindspots(net)
    nearest = min(blind["cells"], key=lambda p: haversine_m(*C, p["lat"], p["lng"]))
    nearest["reach_raw_s"] = nearest["reach_m"] = nearest["reach_s"] = None
    checked = _run(FusionBudgetClient(net, 0, 0), blind=blind, fusion_mode=True)
    assert checked["fusion"]["unknown_inside_ray_km2"] == 0
    assert checked["fusion"]["quality"]["missing_observations"] > 0
    assert checked["fusion"]["pending_points"]
    assert not net.routes and net.matrix_pairs == 0


def test_grid_tooltips_follow_the_field_without_mixing_matrix_and_route_distance():
    grid = {
        "cells": [{"lat": 0.25, "lng": 0.25, "reach_s": 1500, "reach_raw_s": 800, "reach_m": 1000}]
    }
    fusion = {
        "measured_points": [],
        "time_field": {"vertices": [[0, 0, 0], [1, 0, 900], [0, 1, 900]], "triangles": [[0, 1, 2]]},
    }
    sync_heat_measurements(grid, fusion)
    assert grid["cells"][0]["reach_s"] == 450
    assert grid["cells"][0]["reach_source"] == "fusion_interpolated"
    assert grid["cells"][0]["reach_raw_s"] == 800
    fusion["measured_points"] = [
        {
            "lat": 0.25,
            "lng": 0.25,
            "raw_s": 800,
            "time_s": 850,
            "distance_m": 1100,
            "matrix_distance_m": 1000,
            "source": "own_route",
        }
    ]
    sync_heat_measurements(grid, fusion)
    assert grid["cells"][0]["reach_s"] == 450
    assert grid["cells"][0]["reach_observed_s"] == 850
    assert grid["cells"][0]["reach_m"] == 1000
    assert grid["cells"][0]["reach_route_m"] == 1100


@pytest.mark.parametrize("occupied", [False, True])
def test_hole_probe_uses_actual_interior_coordinate_even_when_lattice_key_is_occupied(occupied):
    from app.isochrone.geometry import METERS_PER_DEG_LAT, meters_per_deg_lng, point_in_polygon

    def coordinate(x, y):
        return [C[1] + x / meters_per_deg_lng(C[0]), C[0] + y / METERS_PER_DEG_LAT]

    peak = coordinate(300.2, 0.4)
    hole = [coordinate(x, y) for x, y in ((300.1, 0.3), (300.1, 0.5), (300.3, 0.5), (300.3, 0.3))]
    hole.append(hole[0])
    surface = {
        "geometry": {
            "type": "MultiPolygon",
            "coordinates": [
                [
                    [
                        coordinate(-600, -600),
                        coordinate(600, -600),
                        coordinate(600, 600),
                        coordinate(-600, 600),
                        coordinate(-600, -600),
                    ],
                    hole,
                ]
            ],
        },
        "time_field": {"vertices": [[*peak, 1200]]},
        "unknown_inside_ray_km2": 0,
    }
    net = FakeNet(pocket=False)
    chk = _Checker(net, C, POLY, None, GridcheckConfig(fusion_mode=True))
    lattice = _Lattice([{"lat": C[0], "lng": C[1]}], 100)
    existing = chk.add((0, 24), 0, "cell", lattice=lattice) if occupied else None
    with patch("app.isochrone.gridcheck._estimate", return_value={"fusion": surface}):
        completion = asyncio.run(_complete_fusion(chk, lattice, []))
    probes = [p for p in chk.probes.values() if p.kind == "hole_probe"]
    assert len(probes) == 1
    assert (probes[0].lat, probes[0].lng) == (peak[1], peak[0])
    assert point_in_polygon(
        probes[0].lat, probes[0].lng, {"type": "Polygon", "coordinates": [hole]}
    )
    assert len(net.routes) == completion["route_checks"] == 5  # 峰值加四侧，三轮不重复同一目标。
    assert len({tuple(d) for _, d, _ in net.routes}) == 5
    if occupied:
        assert chk.probes[(0, 24)] is existing and not existing.measured
    # 非格网点不能通过备用 key 获得整块 50 米方格的面积。
    from app.isochrone.gridcheck import _gap_candidates

    probes[0].route_authoritative = True
    probes[0].route_s = 1200
    assert not _gap_candidates(probes, 900)


def test_existing_unverified_hole_peak_is_verified_without_a_duplicate_record():
    lat, lng = offset_point(*C, 90, 300)
    hole = [
        [lng - 1e-5, lat - 1e-5],
        [lng - 1e-5, lat + 1e-5],
        [lng + 1e-5, lat + 1e-5],
        [lng + 1e-5, lat - 1e-5],
    ]
    surface = {
        "geometry": {"type": "MultiPolygon", "coordinates": [[[], [*hole, hole[0]]]]},
        "time_field": {"vertices": [[lng, lat, 1200]]},
        "unknown_inside_ray_km2": 0,
    }
    net = FakeNet(pocket=False)
    chk = _Checker(net, C, POLY, None, GridcheckConfig(fusion_mode=True))
    p = chk.add((0, 24), 0, "cell", lat, lng)
    p.measured, p.raw_s = True, 1200
    lattice = _Lattice([{"lat": C[0], "lng": C[1]}], 100)
    with patch("app.isochrone.gridcheck._estimate", return_value={"fusion": surface}):
        completion = asyncio.run(_complete_fusion(chk, lattice, []))
    assert len(chk.probes) == len(net.routes) == completion["route_checks"] == 5
    assert p.route_authoritative


def test_snapped_hole_probe_is_not_reported_as_a_measurement_inside_the_hole():
    from app.isochrone.geometry import point_in_polygon

    lat, lng = offset_point(*C, 90, 300)
    hole = [
        [lng - 1e-5, lat - 1e-5],
        [lng - 1e-5, lat + 1e-5],
        [lng + 1e-5, lat + 1e-5],
        [lng + 1e-5, lat - 1e-5],
    ]
    net = RouteNet(end_offset=30)
    chk = _Checker(net, C, POLY, None, GridcheckConfig(fusion_mode=True))
    p = chk.add((700000, 0), 1, "hole_probe", lat, lng)
    asyncio.run(chk.verify_by_route(p))
    assert point_in_polygon(p.lat, p.lng, {"type": "Polygon", "coordinates": [hole]})
    measured = estimate_fusion(chk, [])["measured_points"][0]
    assert not point_in_polygon(
        measured["lat"], measured["lng"], {"type": "Polygon", "coordinates": [hole]}
    )
    assert measured["source"] == "relocated_route"
