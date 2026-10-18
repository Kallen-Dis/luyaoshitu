"""首次计算复用、方向覆盖及证据分级；不依赖历史反例缓存，不联网。"""

import asyncio
from types import SimpleNamespace

import pytest
from test_gridcheck import POLY, C, FakeNet

from app.isochrone.completion import reuse_ray_routes, spread_boundary_targets
from app.isochrone.evidence import calibrate_matrix_observations
from app.isochrone.geometry import offset_point
from app.isochrone.gridcheck import GridcheckConfig, _Checker, _gap_candidates, _Lattice
from app.isochrone.localfield import Observation, RayPrior, RouteBound, fit_field


def test_ray_response_reused_at_zero_additional_cost_and_not_counted_as_a_grid():
    net = FakeNet(pocket=False)
    dest = offset_point(*C, 90, 600)
    route = asyncio.run(net.walking_route(C, dest))
    refine = SimpleNamespace(targets=[dest], routes=[route], base_speed=1.17, closures=())
    chk = _Checker(net, C, POLY, refine, GridcheckConfig(fusion_mode=True, max_routes=0))
    chk.origin_preflight = {"passed": True}
    lattice = _Lattice([{"lat": C[0], "lng": C[1]}], 100)
    assert reuse_ray_routes(chk, lattice) == 1
    assert len(net.routes) == 1 and chk.budget.routes_used == 0
    point = next(iter(chk.probes.values()))
    assert point.observed_location == dest and point.route_s == route["duration_s"]
    assert reuse_ray_routes(chk, lattice) == 0
    point.route_s = 1200
    assert _gap_candidates([point], 900) == []


def test_ray_reuse_rejects_incomplete_or_different_origin_metadata():
    net = FakeNet(pocket=False)
    dest = offset_point(*C, 90, 600)
    route = asyncio.run(net.walking_route(offset_point(*C, 0, 5), dest))
    refine = SimpleNamespace(targets=[dest], routes=[route], base_speed=1.17, closures=())
    chk = _Checker(net, C, POLY, refine, GridcheckConfig(fusion_mode=True))
    chk.origin_preflight = {"passed": True}
    lattice = _Lattice([{"lat": C[0], "lng": C[1]}], 100)
    assert reuse_ray_routes(chk, lattice) == 0 and not chk.probes


def test_cold_boundary_selection_cannot_spend_first_batch_in_one_direction():
    chk = _Checker(FakeNet(pocket=False), C, POLY, None, GridcheckConfig(fusion_mode=True))
    geometry = {"type": "MultiPolygon", "coordinates": [[[[b, a] for a, b in POLY]]]}
    ordered = [((offset_point(*C, angle, 650)), 900) for angle in [5, 6, 7, 8, 55, 100, 145, 190]]
    result, counts = spread_boundary_targets(chk, {"geometry": geometry}, ordered)
    assert len(set(point for point, _ in result[:4])) == 4
    assert sum(counts) == 0
    assert sum(result.index(item) < 4 for item in ordered[:4]) == 1


def test_raw_prior_keeps_faster_farther_sample_and_does_not_pin_it_to_ray_boundary():
    ring = [(600, -600), (600, 600), (-600, 600), (-600, -600)]
    prior = RayPrior(ring, [(200, 0, 600, 500), (400, 0, 1200, 1000), (600, 0, 800, 800)], 900)
    assert prior.measured_time(400, 0) == 1200
    assert prior.measured_time(600, 0) == 800
    assert prior(600, 0) == pytest.approx(900)  # 无证据处仍是明确标识的射线先验。


def test_reused_route_outside_old_domain_keeps_its_endpoint_and_whole_path():
    ring = [(300, -300), (300, 300), (-300, 300), (-300, -300)]
    observation = Observation(1000, 0, 1200, 80, True, 1000)
    route = RouteBound(((0, 0), (500, 0), (1000, 0)), 1200, 1000)
    vertices, _, quality = fit_field(ring, [], [observation], routes=[route])
    assert any(v.x == 1000 and v.y == 0 and 900 < v.t <= 1200 for v in vertices)
    assert quality["clipped_paths"] == 0 and quality["duplicate_or_outside_observations"] == 0


def matrix_scene(off_road=0, disagreement=False):
    net = FakeNet(pocket=False)
    chk = _Checker(net, C, POLY, None, GridcheckConfig(fusion_mode=True))
    for i, radius in enumerate((450, 500)):
        target = offset_point(*C, 90, radius)
        p = chk.add((i, 0), 0, "cell", *target)
        p.dist_m = net.dist(C, target)
        p.raw_s = p.dist_m / 1.17
        asyncio.run(chk.verify_by_route(p))
        if disagreement and i == 1:
            p.route_s += 100
    target = offset_point(*offset_point(*C, 90, 425), 0, off_road)
    p = chk.add((2, 0), 0, "cell", *target)
    p.dist_m, p.raw_s = net.dist(C, target), net.dist(C, target) / 1.17
    return chk, Observation(*chk.xy(*target), p.raw_s + 500, distance=p.dist_m)


def test_matrix_guidance_requires_two_matching_endpoints_on_same_known_paths():
    chk, observation = matrix_scene()
    [result] = calibrate_matrix_observations(chk, [observation])
    assert result.soft_eligible and not result.protected and result.weight == 2
    assert result.seconds == pytest.approx(chk.probes[(2, 0)].raw_s + 60)


@pytest.mark.parametrize("options", [{"off_road": 5}, {"disagreement": True}])
def test_nearby_road_or_conflicting_wait_cannot_upgrade_matrix_evidence(options):
    chk, observation = matrix_scene(**options)
    assert calibrate_matrix_observations(chk, [observation]) == [observation]
