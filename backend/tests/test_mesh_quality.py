"""局部网格改善不得搬动测点、破坏覆盖，密集证据应在细化后稳定。"""

import math
from collections import Counter

import pytest

from app.isochrone.fieldmesh import FieldMesh
from app.isochrone.localfield import Observation, contour_field, fit_field, hole_evidence
from app.isochrone.tin import ring_area


def test_flips_preserve_vertices_domain_and_manifold_edges():
    mesh = FieldMesh(100, 50)
    for p in ((13, 17), (28, 9), (-27, -31), (0.01, 1), (40, 45)):
        mesh.insert(p)
    before = list(mesh.points)
    assert mesh.improve_quality() > 0
    assert mesh.points == before
    faces = [f for f in mesh.faces if f is not None]
    assert sum(ring_area([mesh.points[k] for k in f]) for f in faces) == pytest.approx(40000)
    edges = Counter(
        tuple(sorted((a, b))) for f in faces for a, b in zip(f, (*f[1:], f[0]), strict=True)
    )
    assert all(count in (1, 2) for count in edges.values())
    assert all(
        any(abs(mesh.points[k][axis]) == 100 for axis in (0, 1))
        for e, n in edges.items()
        if n == 1
        for k in e
    )
    assert all(
        mesh.stencil(p) for p in ((x, y) for x in range(-95, 96, 10) for y in range(-95, 96, 10))
    )


@pytest.mark.parametrize(
    "center,angle,coords,radius",
    [
        ((300, 0), 0, (-40, 0, 40), 120),
        ((313, 17), 0, (-40, 0, 40), 120),
        ((300, 0), 0.37, (-40, 0, 40), 120),
        ((0, 300), 0.71, (-40, 0, 40), 120),
        ((-220, -220), 1.13, (-40, 0, 40), 120),
        ((300, 0), 0, (-80, -40, 0, 40, 80), 200),
        ((273, 23), 0.37, (-80, -40, 0, 40, 80), 200),
    ],
)
def test_dense_hole_stable_under_refinement_and_support_variation(center, angle, coords, radius):
    def xy(x, y):
        return center[0] + x * math.cos(angle) - y * math.sin(angle), center[1] + x * math.sin(
            angle
        ) + y * math.cos(angle)

    ring = [
        (600 * math.cos(i * math.tau / 36), 600 * math.sin(i * math.tau / 36)) for i in range(36)
    ]
    obs = [Observation(*xy(x, y), 1200, 80, True) for x in coords for y in coords]
    obs += [
        Observation(
            *xy(radius * math.cos(i * math.tau / 8), radius * math.sin(i * math.tau / 8)),
            650,
            80,
            True,
        )
        for i in range(8)
    ]
    results = []
    for options in ({}, {"refine": True}, {"support_scale": 0.8}, {"support_scale": 1.2}):
        v, t, q = fit_field(ring, [], obs, **options)
        assert q["ready"] and q["solver_converged"]
        results.append(contour_field(v, t, 900).rings)
    q = hole_evidence(results[0], obs, 900, stable_rings=results[1], sensitivity_rings=results[2:])
    assert q["holes"] == q["supported_holes"] == 1
