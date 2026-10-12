"""受约束局部线性插值（三角网）：合成数据上的面积、障碍与未知。"""

import math
import random

import pytest

from app.isochrone.tin import (
    Vertex,
    areas_m2,
    classify_rings,
    contour_rings,
    delaunay,
    discontinuous,
    interpolate,
    ring_area,
)

V = 1.2  # 米/秒


def _rays_and_grid(
    extent=1500.0, step=100.0, rays=36, radii=(200, 400, 600, 800, 1000, 1200, 1400)
):
    pts = [(0.0, 0.0)]
    for k in range(rays):
        a = 2 * math.pi * k / rays
        pts += [(r * math.sin(a), r * math.cos(a)) for r in radii]
    n = int(extent // step)
    pts += [(i * step + 37.0, j * step + 11.0) for i in range(-n, n + 1) for j in range(-n, n + 1)]
    return pts


def _radial(pts):
    return [Vertex(x, y, math.hypot(x, y) / V, math.hypot(x, y)) for x, y in pts]


def test_delaunay_covers_the_hull_without_overlap():
    rng = random.Random(7)
    pts = [(rng.uniform(0, 1000), rng.uniform(0, 1000)) for _ in range(200)]
    tris = delaunay(pts)
    # 欧拉公式：三角形数 = 2n − 2 − 凸包顶点数
    total = sum(
        abs(
            (pts[b][0] - pts[a][0]) * (pts[c][1] - pts[a][1])
            - (pts[c][0] - pts[a][0]) * (pts[b][1] - pts[a][1])
        )
        / 2
        for a, b, c in tris
    )
    assert 2 * len(pts) - 2 - len(tris) >= 3
    assert total == pytest.approx(_hull_area(pts), rel=1e-6)


def _hull_area(pts):
    pts = sorted(pts)

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    hull = lower[:-1] + upper[:-1]
    return abs(ring_area(hull))


def test_regular_grid_degenerate_cocircular_points_are_handled():
    pts = [(i * 100.0, j * 100.0) for i in range(6) for j in range(6)]
    tris = delaunay(pts)
    assert len(tris) == 2 * 5 * 5  # 每个方格两块，不重叠也不缺
    assert delaunay(list(reversed(pts))) and len(delaunay(list(reversed(pts)))) == len(tris)


def test_radial_field_area_matches_the_circle():
    """T = r / v：900 秒等值线是半径 1080 米的圆。"""
    raster = interpolate(_radial(_rays_and_grid()), res_m=10.0)
    area, unknown = areas_m2(raster, 900.0)
    assert unknown == 0
    assert area == pytest.approx(math.pi * 1080**2, rel=0.01)
    rings = contour_rings(raster, 900.0)
    assert len(rings) == 1
    assert abs(ring_area(rings[0])) == pytest.approx(area, rel=0.01)
    assert classify_rings(rings) == [False]


def test_no_interpolation_across_a_detour_clue():
    """x > 300 的一侧要绕远（路网距离多 1500 米）：普通线性插值会在 300~400 之间拉出一道斜坡，
    受约束插值让分界落在两侧顶点的中垂线附近，不把障碍抹平。"""
    pts = [(x, y) for x in range(0, 701, 100) for y in range(0, 701, 100)]
    verts = []
    for x, y in pts:
        d = math.hypot(x, y) + (1500.0 if x > 300 else 0.0)
        verts.append(Vertex(float(x), float(y), d / V, d))
    assert discontinuous(verts[3 * 8], verts[4 * 8], 2.0, 30.0)
    raster = interpolate(verts, res_m=10.0)
    reach_at = []
    for j in range(raster.nx):
        x, _ = raster.center(0, j)
        v = raster.values[int((300 - raster.y0) / raster.res)][j]
        if 300 < x < 400 and v is not None:
            reach_at.append((x, v <= 900))
    # 中垂线 x = 350 以西取西侧的值（可达），以东取东侧的值（远超 900 秒）
    assert all(ok == (x < 350) for x, ok in reach_at)


def test_unknown_vertices_are_neither_reachable_nor_cut():
    pts = [(x * 100.0, y * 100.0) for x in range(5) for y in range(5)]
    verts = [Vertex(x, y, None if (x, y) == (200.0, 200.0) else 100.0, 120.0) for x, y in pts]
    raster = interpolate(verts, res_m=10.0)
    area, unknown = areas_m2(raster, 900.0)
    assert unknown > 0
    assert area + unknown == pytest.approx(400 * 400, rel=0.02)


def test_long_edges_are_unknown_not_extrapolated():
    verts = [Vertex(0, 0, 0, 0), Vertex(1000, 0, 100, 100), Vertex(0, 1000, 100, 100)]
    raster = interpolate(verts, res_m=20.0, max_edge_m=300.0)
    area, unknown = areas_m2(raster, 900.0)
    assert area == 0 and unknown > 0


def test_blocked_vertex_carves_a_hole_with_nearest_rule():
    """中间一点被围挡挡住（T = inf）：只挖掉离它最近的那一小块，形成孔洞。"""
    pts = [(x * 100.0, y * 100.0) for x in range(-3, 4) for y in range(-3, 4)]
    verts = [Vertex(x, y, math.inf if (x, y) == (0.0, 0.0) else 300.0, 360.0) for x, y in pts]
    raster = interpolate(verts, res_m=5.0)
    rings = contour_rings(raster, 900.0)
    holes = classify_rings(rings)
    assert holes.count(True) == 1 and holes.count(False) == 1
    area, _ = areas_m2(raster, 900.0)
    assert 600 * 600 - area == pytest.approx(100 * 100, rel=0.15)


def test_result_does_not_depend_on_point_order():
    pts = _rays_and_grid(extent=800.0, step=100.0, rays=12, radii=(300, 600, 900))
    a = areas_m2(interpolate(_radial(pts), res_m=20.0), 500.0)[0]
    random.Random(3).shuffle(pts)
    b = areas_m2(interpolate(_radial(pts), res_m=20.0), 500.0)[0]
    assert a == pytest.approx(b, rel=0.002)
