"""水域约束通用于任意位置，且不依赖网络。"""

import asyncio
import json
from unittest.mock import patch

import pytest
from shapely.geometry import Polygon, box, mapping, shape

from app.isochrone.algorithm import IsochroneConfig, compute_isochrone
from app.isochrone.domain import GeographicDomain, load_domain
from app.isochrone.geometry import haversine_m
from app.isochrone.localfield import contour_field, fit_field


@pytest.mark.parametrize("center", [(31.2, 121.4), (29.97, 122.33), (35.0, 119.0)])
def test_water_exclusion_and_explicit_deck_are_applied_at_any_center(tmp_path, center):
    lat, lng = center
    coverage = box(lng - 0.1, lat - 0.1, lng + 0.1, lat + 0.1)
    water = box(lng, lat - 0.02, lng + 0.02, lat + 0.02)
    deck = box(lng - 0.001, lat - 0.0001, lng + 0.01, lat + 0.0001)
    path = tmp_path / "water.json"
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "properties": {
                    "source": "test-only",
                    "version": "1",
                    "coord_sys": "bd09",
                    "coverage": mapping(coverage),
                },
                "features": [
                    {"type": "Feature", "properties": {"kind": kind}, "geometry": mapping(g)}
                    for kind, g in [("water", water), ("walkable_deck", deck)]
                ],
            }
        ),
        encoding="utf8",
    )
    domain, status = load_domain(center, 2500, path)
    assert status["status"] == "covered"
    assert not domain.allowed(lat + 0.001, lng + 0.001)
    assert domain.allowed(lat, lng + 0.001)
    result = shape(domain.clip(mapping(box(lng - 0.01, lat - 0.01, lng + 0.01, lat + 0.01))))
    assert result.intersection(water.difference(deck)).area == pytest.approx(0, abs=1e-12)
    assert result.covers(deck.intersection(box(lng - 0.01, lat - 0.01, lng + 0.01, lat + 0.01)))


def test_missing_and_partial_data_are_not_treated_as_water_free(tmp_path):
    path = tmp_path / "domain.json"
    assert load_domain((31, 121), 1000, path)[1]["status"] == "missing"
    path.write_text(json.dumps({"properties": {"coord_sys": "wgs84"}, "features": []}))
    assert load_domain((31, 121), 1000, path)[1]["status"] == "invalid"
    path.write_text(
        json.dumps(
            {
                "properties": {
                    "coord_sys": "bd09",
                    "source": "test",
                    "version": "1",
                    "coverage": mapping(box(121, 31, 121.001, 31.001)),
                },
                "features": [],
            }
        )
    )
    assert load_domain((31, 121), 1000, path)[1]["status"] == "incomplete"


def test_water_cannot_couple_across_mesh_or_enter_contour():
    center = (0, 0)
    domain = GeographicDomain(
        center, box(0.001, -0.01, 0.003, 0.01), box(-1, -1, 1, 1), "test", "1"
    )
    ring = [(-300, -300), (300, -300), (300, 300), (-300, 300)]
    v, faces, quality = fit_field(ring, [], [], domain=domain)
    water = domain.xy_shape(domain.water)
    assert faces and quality["solver_converged"]
    assert all(
        Polygon([(v[k].x, v[k].y) for k in f]).intersection(water).area <= 1e-6 for f in faces
    )
    contour = contour_field(v, faces, 900)
    assert contour.rings


def test_multisegment_routes_quarantine_water_segments_without_filling_water():
    from app.isochrone.localfield import RouteBound

    domain = GeographicDomain(
        (0, 0), box(0.001, -0.01, 0.003, 0.01), box(-1, -1, 1, 1), "test", "1"
    )
    ring = [(-300, -300), (300, -300), (300, 300), (-300, 300)]
    path = tuple((x, 0) for x in range(0, 301, 10))
    with patch.object(domain, "xy_shape", wraps=domain.xy_shape) as projected:
        v, faces, quality = fit_field(
            ring, [], [], routes=[RouteBound(path, 500, 300)], domain=domain
        )
    # 与段数无关：网格边界一次，整段路线核验共用一次；冲突水面段不用于路径约束。
    assert projected.call_count <= 2
    assert quality["water_path_conflicts"] > 0
    assert quality["clipped_paths"] == 0
    # 已知水面仍完全排除；并未用可能的桥面路线将水面染绿。
    water = domain.xy_shape(domain.water)
    assert all(
        Polygon([(v[k].x, v[k].y) for k in f]).intersection(water).area <= 1e-6 for f in faces
    )


def test_clipping_entirely_water_produces_empty_valid_surface():
    domain = GeographicDomain((0, 0), box(-1, -1, 1, 1), box(-2, -2, 2, 2), "test", "1")
    clipped = domain.clip(mapping(box(-0.1, -0.1, 0.1, 0.1)))
    assert clipped == {"type": "MultiPolygon", "coordinates": []}


def test_realtime_sampling_and_formal_area_use_same_domain():
    center = (31.2, 121.4)
    domain = GeographicDomain(
        center, box(121.401, 31.1, 121.5, 31.3), box(121, 31, 122, 32), "test", "1"
    )

    class Net:
        async def route_matrix(self, mode, origin, points):
            assert all(domain.allowed(*p) for p in points)
            return [
                {"duration_s": haversine_m(*origin, *p), "distance_m": haversine_m(*origin, *p)}
                for p in points
            ]

    with patch("app.isochrone.domain.load_domain", return_value=(domain, domain.metadata())):
        iso = asyncio.run(
            compute_isochrone(Net(), center, IsochroneConfig(directions=8, radii_m=(100, 200, 300)))
        )
    result = iso.to_geojson()
    assert result["properties"]["quality"]["water_excluded_samples"] > 0
    assert result["properties"]["failed_points"] == 0
    assert shape(result["geometry"]).intersection(domain.water).area <= 1e-12
    assert result["properties"]["area_m2"] == pytest.approx(
        domain.area(result["geometry"]), abs=0.1
    )


def test_mapped_lake_hole_is_not_treated_as_unverified_route_timeout():
    from app.isochrone.localfield import hole_evidence

    domain = GeographicDomain(
        (0, 0), box(-0.001, -0.001, 0.001, 0.001), box(-1, -1, 1, 1), "test", "1"
    )
    hole = [(-50, -50), (-50, 50), (50, 50), (50, -50)]
    result = hole_evidence([hole], [], 900, domain=domain)
    assert result["unsupported_holes"] == 0 and result["supported_holes"] == 1
    assert result["hole_records"][0]["status"] == "geographic"


@pytest.mark.parametrize("offset, supported", [(0.4e-6, True), (0.01, False)])
def test_lake_hole_accepts_only_mesh_rounding_error(offset, supported):
    from app.isochrone.localfield import hole_evidence

    domain = GeographicDomain((0, 0), box(-1, -1, 1, 1), box(-2, -2, 2, 2), "test", "1")
    hole = [(-50 - offset, -50), (-50 - offset, 50), (50, 50), (50, -50)]
    with patch.object(domain, "xy_shape", return_value=box(-50, -50, 50, 50)):
        result = hole_evidence([hole], [], 900, domain=domain)
    assert result["supported_holes"] == int(supported)
    if supported:
        assert result["hole_records"][0]["status"] == "geographic"


def test_reliable_route_endpoint_inside_water_is_a_conflict_not_silently_discarded():
    from test_gridcheck import POLY, C, FakeNet, _rays

    from app.isochrone.fusion import estimate_fusion
    from app.isochrone.geometry import offset_point
    from app.isochrone.gridcheck import GridcheckConfig, _Checker

    lat, lng = offset_point(*C, 90, 300)
    domain = GeographicDomain(
        C,
        box(lng - 0.0001, lat - 0.0001, lng + 0.0001, lat + 0.0001),
        box(C[1] - 0.1, C[0] - 0.1, C[1] + 0.1, C[0] + 0.1),
        "test",
        "1",
    )
    net = FakeNet(pocket=False)
    chk = _Checker(net, C, POLY, None, GridcheckConfig(fusion_mode=True, domain=domain))
    p = chk.add((0, 24), 0, "cell", lat, lng)
    asyncio.run(chk.verify_by_route(p))
    result = estimate_fusion(chk, _rays(net))
    assert result["quality"]["domain_route_conflicts"] == 1
    assert not result["quality"]["ready"]
