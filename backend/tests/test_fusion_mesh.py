"""直接三角形裁切的几何精度、孔洞、待核实与单边绕行约束。"""

import math

import pytest

from app.isochrone.tin import (
    Vertex,
    clip_mesh,
    contour_mesh,
    inside_ring,
    merge_fragments,
    ring_area,
)


def test_linear_time_boundary_is_exact_without_raster_rounding():
    vertices = [Vertex(0, 0, 840, 0), Vertex(100, 0, 960, 100), Vertex(0, 100, 840, 100)]
    result = contour_mesh(vertices, 900)
    assert result.area_m2 == pytest.approx(3750)
    assert result.unknown_m2 == 0
    assert len(result.rings) == 1
    assert any(x == 50 for x, _ in result.rings[0])
    assert sum(ring_area(r) for r in result.rings) == pytest.approx(result.area_m2)


def test_union_preserves_a_small_measured_hole():
    fragments = []
    for x in range(3):
        for y in range(3):
            if (x, y) == (1, 1):
                continue
            fragments.append([(x, y), (x + 1, y), (x + 1, y + 1), (x, y + 1)])
    rings = merge_fragments(fragments)
    assert sorted(ring_area(r) for r in rings) == [-1, 9]


def test_touching_only_at_a_point_keeps_disconnected_components():
    rings = merge_fragments([[(0, 0), (1, 0), (1, 1), (0, 1)], [(1, 1), (2, 1), (2, 2), (1, 2)]])
    assert len(rings) == 2
    assert all(ring_area(r) == 1 for r in rings)


@pytest.mark.parametrize("invalid", [None, math.inf, math.nan])
def test_unknown_is_a_separate_surface_not_an_over_time_claim(invalid):
    result = contour_mesh(
        [Vertex(0, 0, 100, 0), Vertex(100, 0, invalid, None), Vertex(0, 100, 100, 100)], 900
    )
    assert not result.rings
    assert result.unknown_m2 == 5000
    assert len(result.unknown_rings) == 1


def test_one_discontinuous_edge_cannot_be_reconnected_via_the_third_vertex():
    # AB 跳变强；AC、BC 各自未过阈值，旧图连通方式会借 C 把 A/B 又连起来。
    vertices = [Vertex(0, 0, 100, 0), Vertex(100, 0, 400, 400), Vertex(50, 100, 250, 200)]
    result = contour_mesh(vertices, 250)
    assert result.barrier_triangles == 1
    assert not result.rings
    assert result.unknown_m2 == 5000


def test_detour_between_reachable_samples_does_not_erase_the_interior():
    vertices = [Vertex(0, 0, 100, 0), Vertex(100, 0, 400, 400), Vertex(50, 100, 250, 200)]
    result = contour_mesh(vertices, 900)
    assert result.area_m2 == 5000
    assert result.unknown_m2 == 0
    assert result.barrier_triangles == 0


def test_detour_between_overtime_samples_is_not_missing_measurement():
    vertices = [Vertex(0, 0, 1000, 0), Vertex(100, 0, 1400, 400), Vertex(50, 100, 1200, 200)]
    result = contour_mesh(vertices, 900)
    assert result.area_m2 == 0
    assert result.unknown_m2 == 0
    assert len(result.valid_triangles) == 1


def test_inner_contour_uses_the_main_threshold_guard_and_same_time_field():
    vertices = [Vertex(0, 0, 100, 0), Vertex(100, 0, 400, 400), Vertex(50, 100, 250, 200)]
    main = contour_mesh(vertices, 900)
    inner = contour_mesh(vertices, 250, guard_target=900)
    assert inner.valid_triangles == main.valid_triangles
    assert inner.area_m2 == pytest.approx(2500)
    assert inner.unknown_m2 == 0

    vertices[1] = Vertex(100, 0, 1000, 400)
    main = contour_mesh(vertices, 900)
    inner = contour_mesh(vertices, 250, guard_target=900)
    assert inner.valid_triangles == main.valid_triangles == []
    assert inner.unknown_rings == main.unknown_rings


def test_long_unmeasured_gap_is_not_filled_green():
    result = contour_mesh(
        [Vertex(0, 0, 100, 0), Vertex(1000, 0, 100, 100), Vertex(0, 1000, 100, 100)], 900
    )
    assert result.area_m2 == 0
    assert result.unknown_m2 == 500000


def test_measured_barrier_is_estimated_on_each_side_instead_of_erasing_the_triangle():
    vertices = [Vertex(0, 0, 100, 0), Vertex(100, 0, 1000, 1000), Vertex(50, 100, 250, 200)]
    mesh = contour_mesh(vertices, 900, resolve_barriers=True)
    assert mesh.unknown_m2 == 0
    assert mesh.barrier_triangles == 1
    assert not any(inside_ring(85, 15, ring) for ring in mesh.rings)
    assert any(inside_ring(5, 5, ring) for ring in mesh.rings)
    inner = clip_mesh(mesh, 300)
    assert inner.valid_triangles == mesh.valid_triangles
    assert inner.field_vertices is mesh.field_vertices
    assert inner.area_m2 <= mesh.area_m2


def test_partial_shared_edges_merge_without_holes_or_doubled_area():
    fragments = [
        [(0, 0), (100, 0), (100, 100), (0, 100)],
        [(100, 0), (150, 0), (150, 50), (100, 50)],
    ]
    rings = merge_fragments(fragments)
    assert len(rings) == 1
    assert ring_area(rings[0]) == 12500


def test_barrier_and_linear_neighbour_share_a_valid_union_boundary():
    mesh = contour_mesh(
        [
            Vertex(0, 0, 100, 0),
            Vertex(100, 0, 1000, 1000),
            Vertex(0, 100, 100, 100),
            Vertex(100, 100, 850, 200),
        ],
        900,
        resolve_barriers=True,
    )
    assert mesh.unknown_m2 == 0
    assert sum(ring_area(ring) for ring in mesh.rings) == pytest.approx(mesh.area_m2, abs=1e-3)
