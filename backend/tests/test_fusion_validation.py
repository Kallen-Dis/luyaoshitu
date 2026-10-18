"""核验直接走路线、端点独立、覆盖规划与预留配额的行为测试。"""

import asyncio
from unittest.mock import patch

import pytest
from test_gridcheck import POLY, C, FakeNet, _blindspots, _rays

from app.isochrone.budget import FusionBudgetClient
from app.isochrone.fusion import estimate_fusion
from app.isochrone.geometry import offset_point
from app.isochrone.gridcheck import GridcheckConfig, _Checker, _Lattice, _validate_fusion
from app.isochrone.validation import project_to_walk, walking_segments


def test_validation_needs_no_matrix_request_and_never_changes_fitted_surface():
    class NoMatrix(FakeNet):
        async def walking_matrix_grid(self, *args, **kwargs):
            raise AssertionError("最终核验不应调用矩阵")

    net = NoMatrix(pocket=False)
    cells = _blindspots(net)["cells"]
    lattice = _Lattice(cells, 100)
    chk = _Checker(FusionBudgetClient(net, 0, 20), C, POLY, None, GridcheckConfig(fusion_mode=True))
    for cell in cells:
        iy, ix = lattice.index(cell["lat"], cell["lng"])
        p = chk.add((iy * 8, ix * 8), 0, "cell", cell["lat"], cell["lng"])
        p.measured = True
        p.raw_s, p.dist_m = cell["reach_raw_s"], cell["reach_m"]
    surface = estimate_fusion(chk, _rays(net))
    result = asyncio.run(
        _validate_fusion(chk, lattice, list(chk.probes.values()), {"fusion": surface}, _rays(net))
    )
    assert result["checked"] == result["requested"] - result["unattempted"]
    assert result["points"][-1]["significant_conflict"]
    assert result["unattempted"] > 0  # 明显反例出现后停止付费，不再强求完整覆盖。
    assert result["requested"] >= 8  # 实际范围差异可能包含节点检查未发现的细片区。
    assert net.matrix_pairs == 0
    # 四方向优先的计划不等于四方向均完成；首点反例早停不能虚报方向覆盖。
    assert result["coverage"]["checked_sectors"] == len(
        {p["sector"] for p in result["points"] if p["verdict"] != "unknown"}
    )
    assert not result["coverage"]["complete"]
    assert estimate_fusion(chk, _rays(net))["geometry"] == surface["geometry"]


def test_planned_region_points_are_inside_the_actual_changed_patch():
    from app.isochrone.geometry import point_in_polygon
    from app.isochrone.validation import validation_plan

    net = FakeNet(pocket=False)
    cells = _blindspots(net)["cells"]
    chk = _Checker(net, C, POLY, None, GridcheckConfig(fusion_mode=True))
    lattice = _Lattice(cells, 100)
    leaves = [chk.add((i, i), 0, "cell", p["lat"], p["lng"]) for i, p in enumerate(cells)]
    lat, lng = offset_point(*C, 90, 300)
    slow = chk.add((700100, 0), 1, "hole_probe", lat, lng)
    slow.measured = slow.route_authoritative = True
    slow.route_s = 1200
    slow.route_m = 1400
    leaves.append(slow)  # 实测改变范围，不能拿网格的数值误差当作测试片区。
    surface = estimate_fusion(chk, _rays(net))
    selections, plan = validation_plan(chk, lattice, leaves, surface, _rays(net))
    assert plan["required_regions"] > 0
    targeted = [q for q in selections if q["region"] is not None]
    assert targeted
    for q in targeted:
        assert any(
            point_in_polygon(q["lat"], q["lng"], {"type": "Polygon", "coordinates": [p]})
            for p in q["region_polygons"]
        )


def test_shift_to_training_position_does_not_count_as_independent_validation():
    class Snapped(FakeNet):
        async def walking_route(self, origin, destination, **kwargs):
            closest = min(
                cells,
                key=lambda c: (c["lat"] - destination[0]) ** 2 + (c["lng"] - destination[1]) ** 2,
            )
            return await super().walking_route(origin, (closest["lat"], closest["lng"]), **kwargs)

    net = Snapped(pocket=False)
    cells = _blindspots(net)["cells"]
    lattice = _Lattice(cells, 100)
    chk = _Checker(net, C, POLY, None, GridcheckConfig(fusion_mode=True))
    for cell in cells:
        iy, ix = lattice.index(cell["lat"], cell["lng"])
        p = chk.add((iy * 8, ix * 8), 0, "cell", cell["lat"], cell["lng"])
        p.measured = True
        p.raw_s, p.dist_m = cell["reach_raw_s"], cell["reach_m"]
    surface = estimate_fusion(chk, _rays(net))
    result = asyncio.run(
        _validate_fusion(chk, lattice, list(chk.probes.values()), {"fusion": surface}, _rays(net))
    )
    assert result["checked"] == 0
    assert not result["coverage"]["complete"]


def test_walk_projection_stays_on_path_away_from_training_and_within_target_neighborhood():
    segments = [((0, 0), (80, 0))]
    assert project_to_walk(40, 20, segments, [(0, 0), (80, 0)]) == (40, 0)
    assert project_to_walk(5, 20, segments, [(0, 0)]) is None
    assert project_to_walk(40, 60, segments, []) is None


def test_validation_paths_and_long_placeholder_lines_cannot_seed_new_holdouts():
    from types import SimpleNamespace

    def probe(kind, path):
        return SimpleNamespace(kind=kind, route_path=path, blocked=False)

    chk = SimpleNamespace(
        xy=lambda x, y: (x, y),
        probes={
            0: probe("cell", [(0, 0), (80, 0), (500, 0)]),
            1: probe("validation", [(0, 10), (80, 10)]),
        },
    )
    assert walking_segments(chk) == [((0, 0), (80, 0))]


def test_unusable_endpoint_can_use_frozen_alternative_but_conflict_cannot():
    for snap in (True, False):

        class Net(FakeNet):
            async def walking_route(self, origin, destination, **kwargs):
                target = self.training if self.snap and not self.routes else destination
                route = await super().walking_route(origin, target, **kwargs)
                route["duration_s"] = 1200
                return route

        net = Net(pocket=False)
        chk = _Checker(net, C, POLY, None, GridcheckConfig(fusion_mode=True))
        cells = _blindspots(net)["cells"]
        lattice = _Lattice(cells, 100)
        training = offset_point(*C, 90, 300)
        net.training, net.snap = training, snap
        surface = estimate_fusion(chk, _rays(net))
        surface["measured_points"] = [{"lat": training[0], "lng": training[1], "time_s": 500}]

        def selection(key, radius):
            lat, lng = offset_point(*C, 90, radius)
            return {
                "key": key,
                "lat": lat,
                "lng": lng,
                "source": "walking_path",
                "region": None,
                "sector": 0,
            }

        first = selection((100, 100), 325)
        first["alternatives"] = [selection((101, 101), 400)]
        first["region_nodes"] = []
        plan = {"required_regions": 0, "required_sectors": 4}
        with patch("app.isochrone.gridcheck.validation_plan", return_value=([first], plan)):
            result = asyncio.run(
                _validate_fusion(chk, lattice, [], {"fusion": surface}, _rays(net))
            )
        assert result["disagreements"] == 1
        assert len(net.routes) == (2 if snap else 1)
        assert len(result["points"][0]["attempts"]) == len(net.routes)
        assert estimate_fusion(chk, _rays(net))["geometry"] == surface["geometry"]


@pytest.mark.parametrize("side_m", [0.4, 4.0, 50.0])
@pytest.mark.parametrize("shift_outside", [False, True])
def test_narrow_metric_region_is_checked_without_accepting_an_outside_endpoint(
    side_m, shift_outside
):
    from app.isochrone.geometry import METERS_PER_DEG_LAT, meters_per_deg_lng

    lat, lng = offset_point(*C, 90, 300)
    dx, dy = side_m / meters_per_deg_lng(C[0]), side_m / METERS_PER_DEG_LAT
    face = [[lng, lat], [lng + dx, lat], [lng, lat + dy]]
    target = (lat + dy / 4, lng + dx / 4)

    class Net(FakeNet):
        async def walking_route(self, origin, destination, **kwargs):
            actual = (lat + dy * 0.75, lng + dx * 0.75) if shift_outside else destination
            return await super().walking_route(origin, actual, **kwargs)

    net = Net(pocket=False)
    chk = _Checker(net, C, POLY, None, GridcheckConfig(fusion_mode=True))
    selection = {
        "key": (800000, 0),
        "lat": target[0],
        "lng": target[1],
        "source": "changed_patch",
        "region": 0,
        "sector": 0,
        "region_nodes": face,
        "region_triangles": [face],
        "region_polygons": [[*face, face[0]]],
        "alternatives": [],
    }
    surface = {
        "geometry": {"type": "Polygon", "coordinates": [[*face, face[0]]]},
        "time_field": {"vertices": [[*p, 500] for p in face], "triangles": [[0, 1, 2]]},
        "measured_points": [],
        "origin_anchor": list(C),
    }
    with patch(
        "app.isochrone.gridcheck.validation_plan",
        return_value=([selection], {"required_regions": 1, "required_sectors": 4}),
    ):
        result = asyncio.run(_validate_fusion(chk, None, [], {"fusion": surface}, []))
    assert result["checked"] == (0 if shift_outside else 1)
    assert result["points"][0]["verdict"] == ("unknown" if shift_outside else "consistent")
