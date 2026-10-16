"""主几何可包含孔洞与分离片；改变范围不能平移原来的采样格网。"""

import pytest

from app.isochrone.geometry import (
    METERS_PER_DEG_LAT,
    grid_points,
    meters_per_deg_lng,
    point_in_polygon,
    polygon_area_m2,
    polygon_of,
)

C = (31.25, 121.42)


def square(x, y, size):
    return [
        [C[1] + px / meters_per_deg_lng(C[0]), C[0] + py / METERS_PER_DEG_LAT]
        for px, py in ((x, y), (x + size, y), (x + size, y + size), (x, y + size), (x, y))
    ]


def at(x, y):
    return C[0] + y / METERS_PER_DEG_LAT, C[1] + x / meters_per_deg_lng(C[0])


def domain():
    return {
        "type": "MultiPolygon",
        "coordinates": [[square(0, 0, 400), square(100, 100, 100)], [square(600, 0, 100)]],
    }


def test_membership_respects_holes_and_all_components():
    geometry = domain()
    assert point_in_polygon(*at(50, 50), geometry)
    assert not point_in_polygon(*at(150, 150), geometry)
    assert point_in_polygon(*at(650, 50), geometry)
    assert not point_in_polygon(*at(500, 50), geometry)
    assert polygon_of({"geometry": geometry}) is geometry


def test_area_subtracts_holes_and_adds_disconnected_pieces():
    assert polygon_area_m2(domain()) == pytest.approx(400**2, rel=0.0001)


def test_single_polygon_holes_are_not_discarded():
    geometry = {"type": "Polygon", "coordinates": domain()["coordinates"][0]}
    assert polygon_of({"geometry": geometry}) is geometry
    assert not point_in_polygon(*at(150, 150), geometry)


def test_grid_uses_all_components_and_never_places_points_in_holes():
    geometry = domain()
    points = grid_points(geometry, 50)
    assert any(lng > at(600, 0)[1] for _, lng in points)
    assert all(point_in_polygon(lat, lng, geometry) for lat, lng in points)


def test_extended_grid_stays_aligned_to_original_ray_grid():
    reference = [(p[1], p[0]) for p in square(0, 0, 400)]
    expanded = {"type": "Polygon", "coordinates": [square(-75, -75, 650)]}
    old = grid_points(reference, 100)
    new = grid_points(expanded, 100, alignment=reference)
    assert old
    assert {(round(a, 8), round(b, 8)) for a, b in old}.issubset(
        {(round(a, 8), round(b, 8)) for a, b in new}
    )


def test_old_single_ring_api_remains_compatible():
    ring = square(0, 0, 400)
    old = [(p[1], p[0]) for p in ring]
    assert polygon_of({"geometry": {"type": "Polygon", "coordinates": [ring]}}) == old
