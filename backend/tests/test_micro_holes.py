import math

import pytest
from shapely.geometry import Polygon, box

from app.isochrone.adoption import candidate_hash
from app.isochrone.domain import GeographicDomain
from app.isochrone.fieldmesh import FieldMesh
from app.isochrone.localfield import Observation, contour_field
from app.isochrone.microholes import regularize_micro_holes
from app.isochrone.tin import Vertex, ring_area


def case(*, peak=920, stretch=(1, 1), base=850):
    mesh = FieldMesh(30, 5)
    vertices = [
        Vertex(x * stretch[0], y * stretch[1], peak if (x, y) == (0, 0) else base, None)
        for x, y in mesh.points
    ]
    return vertices, mesh.faces


def domain(water=None):
    return GeographicDomain(
        (0, 0), water if water is not None else Polygon(), box(-1, -1, 1, 1), "test", "1"
    )


def holes(vertices, faces):
    return [r for r in contour_field(vertices, faces, 900).rings if ring_area(r) < 0]


def test_small_unmeasured_hole_is_filled_in_field_with_unchanged_outer_boundary():
    vertices, faces = case()
    before = contour_field(vertices, faces, 900)
    result, audit = regularize_micro_holes(vertices, faces, [], 900, domain=domain())
    after = contour_field(result, faces, 900)
    assert len(holes(vertices, faces)) == 1 and not holes(result, faces)
    assert audit["filled_count"] == 1 and audit["adjusted_vertices"] == 1
    assert after.area_m2 - before.area_m2 == pytest.approx(audit["filled_area_m2"], abs=0.001)
    assert [r for r in before.rings if ring_area(r) > 0] == [
        r for r in after.rings if ring_area(r) > 0
    ]
    assert all(v.t <= 900 for v in result)
    assert vertices[len(vertices) // 2].t == 920  # 不修改原输入或测量记录。


@pytest.mark.parametrize(
    "water,observations,kwargs,reason",
    [
        (box(-0.00001, -0.00001, 0.00001, 0.00001), [], {}, "geographic_exclusion"),
        (None, [Observation(0, 0, 901, protected=True)], {}, "reliable_slow_observation"),
        (
            None,
            [],
            {"clues": [{"status": "confirmed", "far": (0, 0)}]},
            "confirmed_detour_evidence",
        ),
        (None, [], {"area_limit": 0}, "total_adjustment_limit"),
    ],
)
def test_evidence_and_total_budget_block_filling(water, observations, kwargs, reason):
    vertices, faces = case()
    result, audit = regularize_micro_holes(
        vertices, faces, observations, 900, domain=domain(water), **kwargs
    )
    assert result == vertices and audit["filled_count"] == 0
    assert audit["records"][0]["reason"] == reason


def test_long_thin_hole_is_not_filled_despite_small_area():
    vertices, faces = case(stretch=(6, 0.1), base=899)
    assert abs(ring_area(holes(vertices, faces)[0])) < 100
    result, audit = regularize_micro_holes(vertices, faces, [], 900, domain=domain())
    assert result == vertices and audit["records"][0]["reason"] == "span_limit"


@pytest.mark.parametrize("peak", [1001, math.inf])
def test_large_time_spike_or_infinite_value_cannot_be_hidden(peak):
    vertices, faces = case(peak=peak)
    result, audit = regularize_micro_holes(vertices, faces, [], 900, domain=domain())
    assert result == vertices and audit["filled_count"] == 0


def test_missing_water_data_keeps_fill_bounded_and_records_pending_verification():
    vertices, faces = case()
    result, audit = regularize_micro_holes(vertices, faces, [], 900)
    assert audit["filled_count"] == 1 and not holes(result, faces)
    assert audit["geographic_verification_pending"]


def test_hard_lower_bound_is_preserved():
    vertices, faces = case()
    lower = [0] * len(vertices)
    lower[len(vertices) // 2] = 910
    result, audit = regularize_micro_holes(vertices, faces, [], 900, domain=domain(), lower=lower)
    assert result == vertices and audit["records"][0]["reason"] == "hard_lower_bound"


def test_nonempty_five_ten_fifteen_contours_remain_nested_in_the_same_field():
    from shapely.geometry import shape

    from app.isochrone.fusion import rings_geometry

    mesh = FieldMesh(100, 5)
    vertices = [
        Vertex(x, y, 920 if (x, y) == (40, 0) else math.hypot(x, y) * 10, None)
        for x, y in mesh.points
    ]
    result, audit = regularize_micro_holes(vertices, mesh.faces, [], 900, domain=domain())
    assert audit["filled_count"] == 1
    surfaces = [
        shape(rings_geometry(contour_field(result, mesh.faces, t).rings, (0, 0)))
        for t in [300, 600, 900]
    ]
    assert all(not s.is_empty and s.is_valid for s in surfaces)
    assert surfaces[0].difference(surfaces[1]).area < 1e-12
    assert surfaces[1].difference(surfaces[2]).area < 1e-12


def test_regularization_audit_is_included_in_candidate_freeze():
    source = {"quality": {"micro_hole_regularization": {"filled_count": 0}}}
    before = candidate_hash(source)
    source["quality"]["micro_hole_regularization"]["filled_count"] = 1
    assert candidate_hash(source) != before
