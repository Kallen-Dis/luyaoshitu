"""路径上限、局部插点、证据冲突与原子发布的独立行为测试，不调用百度。"""

import asyncio
import math
from copy import deepcopy

import pytest

from app.isochrone.fieldmesh import FieldMesh, segment_crosses_ring
from app.isochrone.fusion import publish_fusion
from app.isochrone.localfield import (
    Observation,
    RouteBound,
    contour_field,
    evidence_patches,
    fit_field,
    hole_evidence,
)
from app.isochrone.tin import inside_ring, ring_area
from app.isochrone.validation import distance_to_paths

RING = [(600 * math.cos(k * math.tau / 36), 600 * math.sin(k * math.tau / 36)) for k in range(36)]


@pytest.mark.parametrize("refine", [False, True])
def test_slow_evidence_faces_hold_between_nodes_without_mesh_cracks(refine):
    obs = [Observation(300 + x, y, 1200, 80, True) for x in (-40, 0, 40) for y in (-40, 0, 40)]
    obs += [
        Observation(
            300 + 120 * math.cos(i * math.tau / 8), 120 * math.sin(i * math.tau / 8), 650, 80, True
        )
        for i in range(8)
    ]
    v, t, q = fit_field(RING, [], obs, refine=refine)
    assert q["ready"] and q["conforming_patch_edges"] > 0
    # 覆盖面内部及公共边，不只重复检查实测节点。
    for face, floor in evidence_patches(obs, (), 900):
        for i in range(7):
            for j in range(7 - i):
                weights = (i / 6, j / 6, 1 - (i + j) / 6)
                x, y = (
                    sum(w * p[axis] for w, p in zip(weights, face, strict=True)) for axis in (0, 1)
                )
                assert value(v, t, x, y) >= floor - 1e-6


def test_reachable_path_vetoes_slow_patch_before_conforming_mesh():
    obs = [Observation(x, y, 1200, 80, True) for x, y in ((260, -40), (340, -40), (300, 40))]
    route = RouteBound(((0, 0), (300, 0), (400, 0)), 800)
    assert evidence_patches(obs, (), 900)
    assert all(floor < 900 for _, floor in evidence_patches(obs, [route], 900))
    v, t, q = fit_field(RING, [], obs, routes=[route], refine=True)
    assert not q["path_violations"]
    assert all(value(v, t, x, 0) <= 800 + 1e-5 for x in range(250, 351, 5))


def value(vertices, triangles, x, y):
    from app.isochrone.fieldmesh import barycentric

    for face in triangles:
        weights = barycentric((x, y), *((vertices[k].x, vertices[k].y) for k in face))
        if weights and min(weights) >= -1e-8:
            return sum(w * vertices[k].t for k, w in zip(face, weights, strict=True))
    raise AssertionError("点不在计算域内")


def test_whole_path_bound_is_not_an_invented_prefix_time_or_fixed_buffer():
    path = ((0, 0), (500, 150), (640, 80))
    v, t, q = fit_field(RING, [], [], routes=[RouteBound(path, 841)])
    assert q["path_constraints"] > 0 and not q["path_violations"]
    for a, b in zip(path, path[1:], strict=False):
        for i in range(101):
            f = i / 100
            assert value(v, t, a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1])) <= 841 + 1e-5
    assert value(v, t, 640, 80) > 600  # 不拿总耗时推造十分钟可达。
    assert value(v, t, 640, 230) > 900  # 不把整条路旁固定宽度染绿。


def test_actual_endpoint_is_a_vertex_and_does_not_spread_slow_time_to_fast_path():
    observations = [Observation(325, 25, 1200, 80, True)]
    route = RouteBound(((0, 0), (300, 0), (380, 0)), 800)
    v, t, q = fit_field(RING, [], observations, routes=[route])
    assert any(abs(p.x - 325) < 1e-7 and abs(p.y - 25) < 1e-7 for p in v)
    assert value(v, t, 325, 25) > 900
    assert value(v, t, 325, 0) <= 800 + 1e-5
    assert not q["path_violations"]
    assert q["computational_spacing_m"] == 50


def test_contradictory_path_and_slow_endpoint_are_not_silently_averaged():
    observation = Observation(300, 0, 1200, 80, True)
    route = RouteBound(((0, 0), (200, 0), (400, 0)), 800)
    *_, q = fit_field(RING, [], [observation], routes=[route])
    assert q["route_response_conflicts"] and not q["ready"]


@pytest.mark.parametrize("end", [400, 700])
def test_local_rollback_does_not_raise_a_known_fast_path_to_the_ray_prior(end):
    route = RouteBound(((0, 0), (300, 0), (end, 0)), 300)
    v, t, q = fit_field(RING, [], [], routes=[route], rollback_xy=[(300, 0), (end, 0)])
    assert q["ready"] and not q["path_violations"]
    assert all(value(v, t, x, 0) <= 300 + 1e-5 for x in range(0, end + 1, 5))


def test_local_rollback_never_replaces_a_reliable_slow_endpoint():
    v, t, q = fit_field(RING, [], [Observation(300, 25, 1200, 80, True)], rollback_xy=[(300, 25)])
    assert q["ready"] and not q["constraint_conflicts"]
    assert value(v, t, 300, 25) > 900


@pytest.mark.parametrize("seconds", [290, 890, 1200])
def test_origin_stays_zero_and_conflicting_origin_measurement_rejects_candidate(seconds):
    for refine in (False, True):
        v, t, q = fit_field(RING, [], [Observation(0, 0, seconds, 80, True)], refine=refine)
        assert value(v, t, 0, 0) == 0
        assert q["constraint_conflicts"] > 0 and not q["ready"]


def test_origin_conflict_is_rejected_even_inside_a_custom_threshold_tolerance():
    v, t, q = fit_field(RING, [], [Observation(0, 0, 190, 80, True)], target=200)
    assert value(v, t, 0, 0) == 0
    assert q["constraint_conflicts"] > 0 and not q["ready"]


def test_valid_zero_origin_observation_preserves_a_usable_field():
    v, t, q = fit_field(RING, [], [Observation(0, 0, 0, 80, True)])
    assert value(v, t, 0, 0) == 0 and q["ready"]


@pytest.mark.parametrize("seconds", [890, 910])
def test_main_borderline_endpoint_cannot_silently_override_ten_minute_class(seconds):
    route = RouteBound(((0, 0), (300, 0), (400, 0)), 550)
    v, t, q = fit_field(RING, [], [Observation(300, 0, seconds, 80, True)], routes=[route])
    assert value(v, t, 300, 0) <= 550 + 1e-5  # 快路线的存在性上限优先，慢响应仍记录冲突。
    assert q["route_response_conflicts"] and not q["ready"]


def test_conflicting_endpoints_are_checked_at_inner_thresholds_too():
    observations = [Observation(300, 0, 590, 80, True), Observation(300, 0, 890, 80, True)]
    *_, q = fit_field(RING, [], observations)
    assert q["constraint_conflicts"] > 0 and not q["ready"]


def test_inserting_a_shared_edge_point_keeps_the_mesh_conforming_and_area_constant():
    mesh = FieldMesh(100, 50)
    original = sum(abs(ring_area([mesh.points[k] for k in face])) for face in mesh.faces)
    for point in ((25, 25), (25, 0), (33, 12), (25, 25)):
        mesh.insert(point)
    faces = [f for f in mesh.faces if f is not None]
    assert math.isclose(sum(abs(ring_area([mesh.points[k] for k in f])) for f in faces), original)
    assert mesh.stencil((33, 12)) == [(mesh.lookup[(33, 12)], 1)]
    edges = {}
    for f in faces:
        for a, b in zip(f, (*f[1:], f[0]), strict=True):
            edge = tuple(sorted((a, b)))
            edges[edge] = edges.get(edge, 0) + 1
    for (a, b), owners in edges.items():
        boundary = any(
            all(abs(p[axis] - side) < 1e-7 for p in (mesh.points[a], mesh.points[b]))
            for axis in (0, 1)
            for side in (-100, 100)
        )
        assert owners == (1 if boundary else 2)


@pytest.mark.parametrize("drift", [-1e-12, 0.0, 1e-12])
def test_almost_on_grid_edge_insertion_splits_both_owners(drift):
    mesh = FieldMesh(100, 50)
    k = mesh.insert((23, 50 + drift))
    # 公共格网边上的节点，两侧均须连接，不留悬空边。
    owners = [f for f in mesh.faces if f is not None and k in f]
    assert any(any(mesh.points[j][1] < 50 - 1e-6 for j in f) for f in owners)
    assert any(any(mesh.points[j][1] > 50 + 1e-6 for j in f) for f in owners)


def test_three_consistent_slow_points_do_not_create_a_fast_gap_between_them():
    observations = [
        Observation(x, y, s, 80, True)
        for x, y, s in ((300, 0, 1200), (340, 0, 1210), (320, 40, 1240))
    ]
    v, t, q = fit_field(RING, [], observations)
    assert q["corroborated_patch_triangles"] > 0 and q["ready"]
    assert value(v, t, 320, 20) > 900
    assert value(v, t, 320, 150) < 900  # 区域之外不传播排除结论。


def test_corroborated_triangle_cannot_exclude_a_known_reachable_path():
    observations = [Observation(x, y, 1200, 80, True) for x, y in ((300, 0), (340, 0), (320, 40))]
    route = RouteBound(((0, 20), (300, 20), (350, 20)), 800)
    v, t, q = fit_field(RING, [], observations, routes=[route])
    assert value(v, t, 320, 20) <= 800 + 1e-5
    assert not q["path_violations"]


def test_sparse_or_borderline_points_cannot_support_an_interpolated_exclusion_patch():
    from app.isochrone.localfield import corroborated_patches
    from app.isochrone.tin import Vertex

    for points in (((300, 0), (500, 0), (400, 200)), ((300, 0), (340, 0), (320, 40))):
        seconds = 1200 if points[1][0] == 500 else 920
        observations = [Observation(x, y, seconds, 80, True) for x, y in points]
        floors, count = corroborated_patches([Vertex(320, 20, 0, None)], observations, [], 900)
        assert floors[0] <= 600.1  # 稀疏或主阈值临界点不能排除十五分钟范围。


def test_a_supported_hole_still_requires_stability_and_no_reachable_path_crossing():
    hole = [(250, -50), (250, 50), (350, 50), (350, -50)]
    points = [Observation(275, 0, 1200, 80, True), Observation(325, 0, 1250, 80, True)]
    points += [Observation(x, y, 650, 80, True) for x, y in ((220, -80), (220, 80), (380, 80))]
    assert hole_evidence([hole], points, 900, stable_rings=[hole])["supported_holes"] == 1
    assert hole_evidence([hole], points, 900)["unsupported_holes"] == 1
    route = RouteBound(((0, 0), (220, 0), (380, 0)), 800)
    evidence = hole_evidence([hole], points, 900, [route], [hole])
    assert evidence["hole_path_crossings"] == [1] and evidence["unsupported_holes"] == 1
    assert segment_crosses_ring((220, 0), (380, 0), hole)


def test_local_refinement_keeps_known_classes_and_nested_contours():
    observations = [Observation(300, 0, 1200, 80, True), Observation(620, 200, 750, 80, True)]
    for refine in (False, True):
        v, t, q = fit_field(RING, [], observations, refine=refine)
        assert q["ready"]
        assert value(v, t, 300, 0) > 900 and value(v, t, 620, 200) < 900
        contours = [contour_field(v, t, limit) for limit in (300, 600, 900)]
        for x in range(-650, 651, 50):
            for y in range(-650, 651, 50):
                inside = [sum(inside_ring(x, y, r) for r in m.rings) % 2 for m in contours]
                assert inside == sorted(inside)


def test_known_route_positions_are_not_independent_validation():
    assert distance_to_paths(300, 0, [((0, 0), (600, 0))]) == 0
    assert distance_to_paths(300, 30, [((0, 0), (600, 0))]) == 30


def test_local_rollback_conforms_nearby_path_without_changing_its_upper_bound():
    route = RouteBound(((0, 0), (300, 10), (630, 10)), 850, 630)
    observation = Observation(630, 10, 850, 80, True, 630)
    vertices, faces, quality = fit_field(
        RING, [], [observation], routes=[route], rollback_xy=[(650, 50)]
    )
    assert quality["conforming_rollback_route_segments"] > 0
    assert quality["constraint_conflicts"] == 0 and quality["path_violations"] == 0
    assert value(vertices, faces, 650, 50) > 900
    for a, b in zip(route.path, route.path[1:], strict=False):
        for i in range(31):
            x, y = (a[k] + i / 30 * (b[k] - a[k]) for k in (0, 1))
            assert value(vertices, faces, x, y) <= 850 + 1e-5


def test_failed_statistics_never_publish_new_geometry_or_partial_statistics():
    from unittest.mock import patch

    feature = {
        "geometry": {"type": "Polygon", "coordinates": []},
        "properties": {"area_km2": 1, "coverage": {"old": True}, "report": {"total": 80}},
    }
    original = deepcopy(feature)

    def accept(candidate, checked):
        candidate["geometry"] = {"type": "MultiPolygon", "coordinates": [[[1, 2]]]}
        candidate["properties"]["area_km2"] = 2
        return True

    async def rebuild(candidate):
        candidate["properties"]["coverage"] = {"partial": True}
        raise RuntimeError("模拟统计重算中断")

    with patch("app.isochrone.fusion.apply_fusion", accept):
        assert not asyncio.run(publish_fusion(feature, {"fusion": {"quality": {}}}, rebuild))
    assert feature["geometry"] == original["geometry"]
    assert feature["properties"]["area_km2"] == 1
    assert feature["properties"]["coverage"] == {"old": True}
    assert feature["properties"]["report"] == {"total": 80}


def test_successful_rebuild_publishes_geometry_and_all_statistics_together():
    from unittest.mock import patch

    feature = {
        "geometry": {"type": "Polygon", "coordinates": []},
        "properties": {"area_km2": 1, "report": {"total": 80}},
    }

    def accept(candidate, checked):
        candidate["geometry"] = {"type": "MultiPolygon", "coordinates": []}
        candidate["properties"]["area_km2"] = 2
        return True

    async def rebuild(candidate):
        assert feature["properties"]["area_km2"] == 1
        candidate["properties"].update(
            coverage={"new": True}, blindspots={"new": True}, report={"total": 82}
        )

    with patch("app.isochrone.fusion.apply_fusion", accept):
        assert asyncio.run(publish_fusion(feature, {}, rebuild))
    assert feature["geometry"]["type"] == "MultiPolygon"
    assert feature["properties"]["area_km2"] == 2
    assert feature["properties"]["report"]["total"] == 82
    assert feature["properties"]["coverage"] == feature["properties"]["blindspots"] == {"new": True}


def test_unclosed_candidate_does_not_crash_analysis_or_claim_valid_geometry():
    from types import SimpleNamespace
    from unittest.mock import patch

    from app.isochrone.fusion import estimate_fusion

    chk = SimpleNamespace(
        center=(0, 0), polygon=RING, xy=lambda a, b: (a, b), cfg=SimpleNamespace(target_s=900)
    )
    with patch("app.isochrone.fusion._estimate_fusion", side_effect=ValueError("边界不闭合")):
        result = estimate_fusion(chk, [])
    assert not result["quality"]["ready"]
    assert result["quality"]["geometry_error"] == "边界不闭合"
    assert result["geometry"]["coordinates"]
