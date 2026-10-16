"""正则项按物理面积计量；检查加密、旋转、钝角单元和局部线索。"""

import math

import pytest

from app.isochrone.fieldenergy import regularization
from app.isochrone.fieldmesh import FieldMesh


def energy(points, faces, values, clues=()):
    diag, rows, mass, _ = regularization(
        points, faces, [None] * len(points), clues, lambda a, b: False
    )
    result = sum(
        diag[k] * v * v + v * sum(c * values[j] for j, c in rows[k].items())
        for k, v in enumerate(values)
    )
    prior = sum(m * v * v for m, v in zip(mass, values, strict=True))
    return result, prior, sum(mass)


@pytest.mark.parametrize("angle", [0, 0.37, 1.13])
def test_constant_prior_and_affine_gradient_energy_do_not_count_inserted_nodes(angle):
    for refined in (False, True):
        mesh = FieldMesh(100, 50)
        if refined:
            for p in ((13, 17), (28, 9), (-27, -31), (0.01, 1), (40, 45)):
                mesh.insert(p)
        points = [
            (x * math.cos(angle) - y * math.sin(angle), x * math.sin(angle) + y * math.cos(angle))
            for x, y in mesh.points
        ]
        faces = [f for f in mesh.faces if f is not None]
        total, prior, mass = energy(points, faces, [1.0] * len(points))
        assert mass == pytest.approx(0.35 * 40000 / 2500)
        assert total == pytest.approx(prior, abs=1e-8)
        total, prior, _ = energy(points, faces, [2 * x - 3 * y + 7 for x, y in points])
        assert total - prior == pytest.approx(2 * 40000 * (2**2 + 3**2), rel=1e-9)


def test_obtuse_triangle_energy_is_nonnegative_and_symmetric():
    points = [(0, 0), (100, 0), (1, 2)]
    diag, rows, _, _ = regularization(points, [(0, 1, 2)], [None] * 3, [], lambda a, b: False)
    assert all(rows[i][j] == pytest.approx(rows[j][i]) for i in range(3) for j in rows[i])
    for values in ([1, -3, 7], [100, 0, -10], [1, 1, 1]):
        assert (
            sum(
                diag[k] * v**2 + v * sum(c * values[j] for j, c in rows[k].items())
                for k, v in enumerate(values)
            )
            > 0
        )


def test_confirmed_clue_reduces_but_never_cuts_local_gradient_energy():
    points = [(0, 0), (100, 0), (50, 50)]
    clue = {"id": 1, "near": (0, 0), "far": (100, 0), "status": "confirmed"}
    full, prior, _ = energy(points, [(0, 1, 2)], [0, 100, 50])
    weaker, weaker_prior, _ = energy(points, [(0, 1, 2)], [0, 100, 50], [clue])
    assert prior == weaker_prior
    assert 0 < weaker - prior < full - prior
