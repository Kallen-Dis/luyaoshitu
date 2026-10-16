"""网格交叉校核：确认才画缺口、子点不投票、细键、绕行线索按 B → A 复测、预算与插值等待。"""

import asyncio
import math

import pytest

from app.isochrone.geometry import (
    METERS_PER_DEG_LAT,
    grid_points,
    haversine_m,
    meters_per_deg_lng,
    offset_point,
    point_in_polygon,
)
from app.isochrone.gridcheck import POINT_GRID_M, GridcheckConfig, cross_check
from app.isochrone.refine import DelayProfile, RefineResult

C = (31.25, 121.42)
SPEED = 1.17
POLY = [offset_point(*C, 10.0 * k, 650.0) for k in range(36)]


def east_north(p):
    return (
        (p[1] - C[1]) * meters_per_deg_lng(C[0]),
        (p[0] - C[0]) * METERS_PER_DEG_LAT,
    )


def in_pocket(p):
    """中心正东 330~700 米、南北 70 米内的一块：围墙挡着，要绕 900 米。
    格心在南北 ±50 米，墙从格内穿过，北半、南半的子点走得到。"""
    x, y = east_north(p)
    return 330 <= x <= 700 and abs(y) <= 70


class FakeNet:
    def __init__(
        self,
        pocket=True,
        contradict=False,
        route_wait_s=60.0,
        route_scale=1.0,
        route_ignores_pocket=False,
    ):
        self.pocket = pocket
        self.contradict = contradict
        self.route_wait_s = route_wait_s
        # 路线规划与批量算路吸附到不同的路上：路线距离按比例缩放，或者干脆不绕那堵墙
        self.route_scale = route_scale
        self.route_ignores_pocket = route_ignores_pocket
        self.calls = []
        self.routes = []
        self.seen = set()
        self.matrix_pairs = 0

    def dist(self, a, b):
        d = haversine_m(*a, *b) * 1.2
        if self.pocket and in_pocket(a) != in_pocket(b):
            d += 900.0
        return d

    def _key(self, o, d, dg, og):
        return (round(o[0], 6), round(o[1], 6), round(d[0], 6), round(d[1], 6), dg, og)

    def missing_pairs(self, mode, origins, destinations, dest_grid_m=None, origin_grid_m=None):
        return sum(
            self._key(o, d, dest_grid_m, origin_grid_m) not in self.seen
            for o in origins
            for d in destinations
        )

    async def walking_matrix_grid(
        self, origins, destinations, dest_grid_m=None, origin_grid_m=None
    ):
        self.calls.append((list(origins), list(destinations), dest_grid_m, origin_grid_m))
        rows = []
        for o in origins:
            row = []
            for d in destinations:
                key = self._key(o, d, dest_grid_m, origin_grid_m)
                if key not in self.seen:
                    self.seen.add(key)
                    self.matrix_pairs += 1
                dist = self.dist(o, d)
                if self.contradict and tuple(o) != C:
                    dist = haversine_m(*o, *d) * 1.2  # 两端吸附到别的路上：直接测反而很近
                row.append({"distance_m": dist, "duration_s": dist / SPEED})
            rows.append(row)
        return rows

    async def walking_route(self, origin, destination, fresh=False, fine_m=None):
        self.routes.append((origin, destination, fine_m))
        if self.route_ignores_pocket:
            dist = haversine_m(*origin, *destination) * 1.2
        else:
            dist = self.dist(origin, destination)
        dist *= self.route_scale
        parts = max(1, math.ceil(haversine_m(*origin, *destination) / 50))
        path = [
            [
                origin[0] + (destination[0] - origin[0]) * i / parts,
                origin[1] + (destination[1] - origin[1]) * i / parts,
            ]
            for i in range(parts + 1)
        ]
        return {
            "distance_m": dist,
            "duration_s": dist / SPEED + self.route_wait_s,
            "steps": [
                {
                    "distance_m": dist,
                    "duration_s": dist / SPEED + self.route_wait_s,
                    "instruction": "过马路",
                    "start_location": {"lat": origin[0], "lng": origin[1]},
                    "end_location": {"lat": destination[0], "lng": destination[1]},
                    "turn_type": "",
                    "path": path,
                }
            ],
        }


def _blindspots(net, exact=True, override=None, spacing=100.0):
    cells = []
    for lat, lng in grid_points(POLY, spacing):
        d = net.dist(C, (lat, lng))
        raw = d / SPEED
        if override and (round(lat, 6), round(lng, 6)) in override:
            raw = override[(round(lat, 6), round(lng, 6))]
        cells.append(
            {
                "lat": lat,
                "lng": lng,
                "reach_s": raw,
                "reach_raw_s": raw,
                "reach_m": d if exact else None,
                "in_circle": True,
            }
        )
    out = {"layout": "polygon", "grid_spacing_m": spacing, "cells": cells}
    if exact:
        out["reach_grid_m"] = 10.0
    return out


def _rays(net):
    out = []
    for k in range(36):
        for r in (200, 400, 600, 800, 1000, 1200, 1400):
            p = offset_point(*C, 10.0 * k, r)
            d = net.dist(C, p)
            out.append((p[0], p[1], d / SPEED, d))
    return out


def _run(net, refine=None, blind=None, **cfg):
    return asyncio.run(
        cross_check(
            net,
            C,
            POLY,
            blind or _blindspots(net),
            refine,
            _rays(net),
            1.3,
            GridcheckConfig(**cfg),
        )
    )


def test_gaps_are_only_confirmed_over_points_inside_the_ray_circle():
    net = FakeNet()
    out = _run(net)
    assert out["gaps"]
    for gap in out["gaps"]:
        for leaf in gap["leaves"]:
            assert leaf["raw_s"] > 900
            assert in_pocket((leaf["lat"], leaf["lng"]))
        # 只凭批量算路判出来的片，也取了最远一点自己的路线核验，路线同样超时
        assert gap["route_confirmed"] >= 1
    assert out["routes"]["representatives"] >= 1
    assert out["disputed"] == {"points": 0, "groups": 0, "area_m2": 0, "outside_points": 0}
    # 射线圈仍是主几何：结果只附带辅助图层，不改动面积口径
    assert out["ray_area_km2"] == 1.3


def test_gap_whose_representative_route_is_short_is_disputed_not_drawn():
    """批量算路说 15 分钟走不到，最远一点自己的路线却只要七八分钟：两者矛盾，整片记存疑。"""
    out = _run(FakeNet(route_ignores_pocket=True))
    assert out["gaps"] == [] and out["gap_area_km2"] == 0
    assert out["disputed"]["groups"] >= 1 and out["disputed"]["points"] > 0
    assert out["disputed"]["area_m2"] > 0
    assert out["routes"]["representatives"] >= 1


def test_route_time_and_matrix_plus_delay_on_opposite_sides_is_disputed():
    """评审指出的口径混用：等待取自这条路线、距离却是批量算路的。路线自己只要不到 15 分钟，
    「批量算路距离 + 路线等待」却超过 15 分钟，这种点不能画成已确认的缺口。"""
    net = FakeNet(pocket=False, route_wait_s=300.0, route_scale=0.8)
    out = _run(net, refine=_uniform_delay(450.0), max_routes=400)
    assert out["over_unverified"] == 0  # 都取到了自己的路线
    assert out["gaps"] == []
    assert out["disputed"]["points"] > 0 and out["disputed"]["groups"] == 0


@pytest.mark.parametrize("spacing", [50.0, 200.0])
def test_leaf_sizes_and_gap_area_follow_the_grid_spacing(spacing):
    """方格边长按实际网格间距算：s、s/2、s/4。写死 100 米时 50 米网格的缺口面积会大 4 倍。"""
    net = FakeNet()
    out = _run(net, blind=_blindspots(net, spacing=spacing), max_pairs=6000)
    sizes = {leaf["size_m"] for g in out["gaps"] for leaf in g["leaves"]}
    assert sizes and sizes <= {spacing, spacing / 2, spacing / 4}
    # 墙后那一块落在射线圈里的部分约 320 × 140 米
    pocket = 0.0
    for x in range(330, 701, 5):
        for y in range(-70, 71, 5):
            lat = C[0] + y / METERS_PER_DEG_LAT
            lng = C[1] + x / meters_per_deg_lng(C[0])
            pocket += 25.0 * point_in_polygon(lat, lng, POLY)
    assert out["gap_area_km2"] * 1e6 <= pocket * 1.6


def test_mixed_cells_keep_their_reachable_parts():
    """格心超时、但子点有的可达：保留差异，不按多数票整格删掉。"""
    out = _run(FakeNet(), max_pairs=3000)
    assert out["stopped"] is None
    assert sum(g["partial_cells"] for g in out["gaps"]) >= 1
    assert out["points"]["sub50"] > 0 and out["points"]["sub25"] > 0


def test_center_measurements_use_fine_destination_keys():
    net = FakeNet()
    _run(net)
    from_center = [c for c in net.calls if c[0] == [C]]
    assert from_center and all(c[2] == POINT_GRID_M and c[3] is None for c in from_center)
    assert all(fine == POINT_GRID_M for _, _, fine in net.routes)


def test_clue_is_measured_from_the_nearer_cell_to_the_farther_one():
    """三角不等式约束的是 B → A（B 离中心近）：复测就测这个方向，不假设双向对称。"""
    net = FakeNet()
    out = _run(net)
    pair_calls = [c for c in net.calls if c[0] != [C]]
    assert pair_calls
    for origins, dests, dest_grid, origin_grid in pair_calls:
        assert dest_grid == POINT_GRID_M and origin_grid == POINT_GRID_M
        assert net.dist(C, origins[0]) < net.dist(C, dests[0])
    assert out["clues"]
    for clue in out["clues"]:
        v = clue["verification"]
        assert clue["status"] == "confirmed"
        assert clue["max_jump_m"] >= 400
        assert v["measured_m"] > 260 and v["bound_m"] == clue["max_jump_m"]


def test_contradicted_clues_are_counted_but_not_shown():
    out = _run(FakeNet(contradict=True))
    assert out["clues"] == []
    assert out["clue_summary"]["contradicted"] >= 1


def test_budget_stops_and_is_reported():
    net = FakeNet()
    out = _run(net, blind=_blindspots(net, exact=False), max_pairs=5)
    assert out["stopped"] == "budget"
    assert out["usage"]["matrix_pairs"] <= 5
    assert net.matrix_pairs <= 5


def _uniform_delay(total_s_per_km):
    profile = DelayProfile(
        knots_s=[0.0, 2000.0], knots_delay=[0.0, 2 * total_s_per_km], length_m=2000
    )
    return RefineResult(
        bearings=[0.0, 90.0, 180.0, 270.0],
        profiles=[profile] * 4,
        base_speed=SPEED,
        delay_applied=True,
        closures=(),
    )


def test_over_only_by_interpolated_delay_is_not_drawn_without_its_own_route():
    """不计等待 ≤ 900 秒、靠插值等待才超时的点：没取到自己的路线就只记「待核实」。"""
    net = FakeNet(pocket=False)
    refine = _uniform_delay(450.0)  # 每公里插值等待 450 秒，600 多米处就超时
    out = _run(net, refine=refine, max_routes=0)
    assert out["gaps"] == []
    assert out["over_unverified"] > 0


def test_own_route_replaces_interpolated_delay():
    net = FakeNet(pocket=False, route_wait_s=20.0)
    out = _run(net, refine=_uniform_delay(450.0), max_routes=200)
    assert out["routes"]["points_verified"] > 0
    assert out["gaps"] == []  # 自己的路线只多 20 秒，都在 15 分钟内
    assert out["over_unverified"] == 0


def test_legacy_coarse_cache_values_are_remeasured():
    """旧结果的格心耗时按 50 米量化取得：重测后纠正，假的超时格不画成缺口。"""
    net = FakeNet(pocket=False)
    lat, lng = grid_points(POLY, 100.0)[40]
    out = _run(
        net, blind=_blindspots(net, exact=False, override={(round(lat, 6), round(lng, 6)): 2249.0})
    )
    assert out["corrected"] == 1
    assert out["gaps"] == []
    assert out["remeasured"] == len(grid_points(POLY, 100.0))


def test_estimate_area_follows_the_measured_field():
    """没有围墙时 T = 1.2·r / v，900 秒等值线半径约 877 米。"""
    out = _run(FakeNet(pocket=False))
    r = 900 * SPEED / 1.2
    assert out["estimate_area_km2"] == pytest.approx(math.pi * r * r / 1e6, rel=0.03)
    assert out["estimate_unknown_km2"] == 0
