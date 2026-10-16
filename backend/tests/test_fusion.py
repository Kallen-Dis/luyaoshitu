"""融合模式真实调用路径：主面、模型口径、未知与核验预算。"""

from copy import deepcopy

import pytest
from test_gridcheck import POLY, C, FakeNet, _rays, _run, east_north

from app.isochrone.budget import FusionBudgetClient
from app.isochrone.fusion import apply_fusion, apply_validation_gate, rings_geometry
from app.isochrone.geometry import polygon_parts
from app.isochrone.localfield import RayPrior


def test_validated_prior_publishes_and_preserves_ray_reference():
    # 成功用例与扇形基础场采用同一坐标和时间口径；球面圆不等于射线多边形。
    class AlignedNet(FakeNet):
        async def walking_route(self, origin, destination, **kwargs):
            route = await super().walking_route(origin, destination, **kwargs)
            prior = RayPrior(
                [east_north(p) for p in POLY],
                [(*east_north((a, b)), t, d) for a, b, t, d in _rays(self)],
                900,
            )
            route["duration_s"] = prior(*east_north(destination))
            route["steps"][0]["duration_s"] = route["duration_s"]
            return route

    # 为起点预检、冻结备选和重试预留预算；不把未知修正片区假定为已验证。
    checked = _run(
        AlignedNet(pocket=False), fusion_mode=True, max_pairs=0, max_routes=64, validation_points=16
    )
    assert checked["version"] == 2
    assert polygon_parts(checked["fusion"]["geometry"])
    assert checked["fusion"]["area_km2"] > 0
    ray = {"type": "Polygon", "coordinates": [[[lng, lat] for lat, lng in POLY]]}
    feature = {"type": "Feature", "geometry": ray, "properties": {"area_km2": 1.3}}
    original = deepcopy(feature)
    assert apply_fusion(feature, checked)
    assert feature["properties"]["fusion"]["ray_geometry"] == original["geometry"]
    assert feature["properties"]["geometry_method"] == "fusion"
    assert feature["properties"]["area_km2"] == checked["fusion"]["area_km2"]
    assert checked["validation"]["requested"] <= checked["validation"]["coverage"]["target_points"]
    assert checked["validation"]["coverage"]["complete"]
    assert checked["usage"]["matrix_pairs"] <= 400
    assert checked["usage"]["routes"] <= 40


def test_a_route_to_the_checked_endpoint_uses_its_own_time_not_mixed_matrix_time():
    checked = _run(FakeNet(route_ignores_pocket=True), fusion_mode=True)
    reliable = [
        p
        for p in checked["fusion"]["measured_points"]
        if p["source"] in ("own_route", "relocated_route")
    ]
    assert reliable and all(
        p["time_basis"] == "route_duration_includes_model_wait" for p in reliable
    )
    # 拒绝模型时，未补测的位置必须留下记录，不能为了满足旧断言删除未知。
    assert checked["completion"]["route_verified_points"] > 0


def test_zero_extra_budget_does_not_silently_spend_to_refine_or_validate():
    net = FakeNet()
    checked = _run(net, fusion_mode=True, max_pairs=0, max_routes=0)
    assert net.matrix_pairs == 0 and not net.routes
    assert checked["stopped"] == "budget"
    assert checked["usage"] == {"matrix_pairs": 0, "routes": 0}


def test_budget_refused_children_do_not_poison_previously_measured_parent_samples():
    baseline = _run(FakeNet(), fusion_mode=True, max_pairs=0, max_routes=0)
    budget = FusionBudgetClient(FakeNet(), 0, 0)
    limited = _run(budget, fusion_mode=True)
    assert limited["fusion"]["geometry"] == baseline["fusion"]["geometry"]
    assert (
        limited["fusion"]["unknown_inside_ray_km2"] == baseline["fusion"]["unknown_inside_ray_km2"]
    )


def test_direct_geometry_groups_holes_with_their_own_component():
    rings = [
        [(0, 0), (100, 0), (100, 100), (0, 100)],
        [(10, 10), (10, 20), (20, 20), (20, 10)],
        [(200, 0), (250, 0), (250, 50), (200, 50)],
    ]
    geometry = rings_geometry(rings, C)
    assert [len(face) for face in geometry["coordinates"]] == [2, 1]


def test_missing_fusion_surface_leaves_the_ray_result_explicitly_labeled():
    feature = {"geometry": {"type": "Polygon", "coordinates": []}, "properties": {"area_km2": 0}}
    assert not apply_fusion(feature, {})
    assert feature["properties"]["geometry_method"] == "ray"
    assert feature["properties"]["fusion_fallback"]


def test_rejected_candidate_preserves_geometry_statistics_and_auditable_reason():
    feature = {
        "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [0, 1], [0, 0]]]},
        "properties": {"area_km2": 1.3, "report": {"total": 84}, "blindspots": {"cells": [1]}},
    }
    original = deepcopy(feature)
    checked = {
        "fusion": {"geometry": {"coordinates": [[[1]]]}, "quality": {"ready": False}},
        "validation": {"disagreements": 1, "checked": 2},
    }
    assert not apply_fusion(feature, checked)
    assert feature["geometry"] == original["geometry"]
    for key in ("area_km2", "report", "blindspots"):
        assert feature["properties"][key] == original["properties"][key]
    assert feature["properties"]["fusion_review"]["validation"]["disagreements"] == 1


@pytest.mark.parametrize("kind", ["Polygon", "MultiPolygon", "later_component"])
def test_main_holes_keep_the_complete_ray_result_even_when_validation_passes(kind):
    import asyncio

    from app.isochrone.fusion import publish_fusion

    outer = [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]]
    hole = [[1, 1], [1, 2], [2, 2], [2, 1], [1, 1]]
    geometry = {"type": "Polygon", "coordinates": [outer, hole]}
    if kind == "MultiPolygon":
        geometry = {"type": kind, "coordinates": [[outer, hole]]}
    elif kind == "later_component":
        other = [[5, 0], [6, 0], [6, 1], [5, 1], [5, 0]]
        geometry = {"type": "MultiPolygon", "coordinates": [[other], [outer, hole]]}
    feature = {
        "geometry": {"type": "Polygon", "coordinates": [outer]},
        "properties": {
            "area_km2": 1.3,
            "area_m2": 1300000,
            "rings": [{"minutes": 10, "coordinates": outer}],
            "coverage": {"categories": {"医药": 2}},
            "blindspots": {"blind_count": 3},
            "report": {"total": 84},
            "gridcheck": {"estimate_rings": [outer, hole]},
        },
    }
    original = deepcopy(feature)
    checked = {
        "target_s": 900,
        "fusion": {
            "geometry": geometry,
            # 故意放入旧的标记：主几何仍须独立检查。
            "quality": {"ready": True, "holes": 0, "unsupported_holes": 0},
        },
        "validation": {"requested": 4, "checked": 4, "coverage": {"complete": True}},
    }

    async def rebuild(candidate):
        pytest.fail("带孔洞的候选不应进入统计重算或新增 API 请求")

    assert not asyncio.run(publish_fusion(feature, checked, rebuild))
    assert feature["geometry"] == original["geometry"]
    for key, value in original["properties"].items():
        assert feature["properties"][key] == value
    props = feature["properties"]
    assert props["geometry_method"] == "ray" and "fusion" not in props
    assert "含 1 个内部孔洞" in props["fusion_fallback"]
    assert props["fusion_review"]["quality"]["main_geometry_holes"] == 1
    assert props["fusion_review"]["quality"]["ready"]  # 展示策略没有伪装成实测失败。
    assert checked["fusion"]["geometry"] == geometry  # 对照仍保留真实孔洞。


@pytest.mark.parametrize("separate_components", [False, True])
def test_hole_free_main_range_can_still_publish_with_inner_time_rings(separate_components):
    outer = [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]]
    hole = [[1, 1], [1, 2], [2, 2], [2, 1], [1, 1]]
    geometry = {"type": "Polygon", "coordinates": [outer]}
    if separate_components:
        other = [[5, 0], [6, 0], [6, 1], [5, 1], [5, 0]]
        geometry = {"type": "MultiPolygon", "coordinates": [[outer], [other]]}
    inner = [{"minutes": 10, "geometry": {"type": "Polygon", "coordinates": [outer, hole]}}]
    checked = {
        "target_s": 900,
        "fusion": {
            "geometry": geometry,
            "quality": {"ready": True},
            "area_km2": 1.2,
            "pending_points": [],
            "rings": inner,
        },
        "validation": {"requested": 4, "checked": 4, "coverage": {"complete": True}},
    }
    feature = {
        "geometry": {"type": "Polygon", "coordinates": [outer]},
        "properties": {"area_km2": 1.3},
    }
    assert apply_fusion(feature, checked)
    assert feature["geometry"] == geometry
    assert feature["properties"]["geometry_method"] == "fusion"
    assert feature["properties"]["fusion"]["quality"]["main_geometry_holes"] == 0
    assert feature["properties"]["rings"] == inner


def test_grid_tooltip_keeps_field_estimate_and_raw_observation_separate():
    from app.isochrone.fusion import sync_heat_measurements

    grid = {"cells": [{"lat": 0.25, "lng": 0.25}]}
    fusion = {
        "time_field": {"vertices": [[0, 0, 0], [1, 0, 900], [0, 1, 900]], "triangles": [[0, 1, 2]]},
        "measured_points": [
            {
                "lat": 0.25,
                "lng": 0.25,
                "time_s": 460,
                "raw_s": 400,
                "distance_m": 500,
                "source": "own_route",
            }
        ],
    }
    sync_heat_measurements(grid, fusion)
    cell = grid["cells"][0]
    assert cell["reach_s"] == 450
    assert cell["reach_observed_s"] == 460
    assert cell["reach_source"] == "fusion_interpolated"


@pytest.mark.parametrize("count", [0, 1, 2])
def test_incomplete_validation_cannot_publish_a_new_geometry(count):
    feature = {"geometry": {"type": "Polygon", "coordinates": []}, "properties": {"area_km2": 1}}
    checked = {
        "fusion": {"geometry": {"coordinates": [[[1]]]}, "quality": {"ready": True}},
        "validation": {"requested": 6, "checked": count, "disagreements": 0},
    }
    assert not apply_fusion(feature, checked)
    assert feature["properties"]["geometry_method"] == "ray"
    assert "至少需 5 个" in feature["properties"]["fusion_fallback"]
    assert feature["properties"]["area_km2"] == 1


def test_validation_gate_respects_the_requested_time_threshold():
    candidate = {"target_s": 600, "quality": {"ready": True}}
    apply_validation_gate(
        candidate,
        {
            "requested": 6,
            "checked": 6,
            "disagreements": 1,
            "points": [{"verdict": "conflict", "route_s": 620}],
            "coverage": {"complete": True},
        },
    )
    assert candidate["quality"]["ready"]  # 10 分钟的临界点，而不是拿它与 900 秒比较。
    apply_validation_gate(
        candidate,
        {
            "requested": 6,
            "checked": 6,
            "disagreements": 1,
            "points": [{"verdict": "conflict", "route_s": 650}],
            "coverage": {"complete": True},
        },
    )
    assert not candidate["quality"]["ready"]


def test_failed_routes_leave_validation_incomplete_and_reject_fusion():
    class FailedRoutes(FakeNet):
        async def walking_route(self, *args, **kwargs):
            return None

    checked = _run(FusionBudgetClient(FailedRoutes(pocket=False), 400, 40), fusion_mode=True)
    assert checked["validation"]["checked"] == 0
    assert checked["fusion"]["quality"]["validation_insufficient"]
    assert not checked["fusion"]["quality"]["ready"]


def test_spatial_validation_catches_an_underestimated_boundary_between_fit_points():
    from unittest.mock import patch

    from app.isochrone.gridcheck import _validate_fusion

    class UnseenDetour(FakeNet):
        validating = False

        async def walking_route(self, origin, destination, **kwargs):
            route = await super().walking_route(origin, destination, **kwargs)
            if self.validating:
                # 未参与拟合的新位置出现额外绕行，冻结后的模型必须拒绝而非吸收它。
                route["duration_s"] += 600
                route["steps"][0]["duration_s"] += 600
            return route

    net = UnseenDetour(pocket=False)

    async def validate(*args, **kwargs):
        net.validating = True
        return await _validate_fusion(*args, **kwargs)

    with patch("app.isochrone.gridcheck._validate_fusion", validate):
        # 候选须先通过预审，才能测试冻结后的新点冲突；不依赖一个本来就被拒绝的开发场。
        checked = _run(net, fusion_mode=True, max_pairs=0, max_routes=64, completion_rounds=0)
    assert checked["fusion"]["quality"]["validation_significant_conflicts"] > 0
    assert not checked["fusion"]["quality"]["ready"]


def test_enough_points_in_one_area_do_not_replace_coverage_of_other_changed_regions():
    estimate = {"quality": {"ready": True}}
    apply_validation_gate(
        estimate,
        {
            "requested": 8,
            "checked": 8,
            "disagreements": 0,
            "coverage": {"complete": False, "missing_regions": [1]},
        },
    )
    assert not estimate["quality"]["ready"]
    assert not estimate["quality"]["validation_insufficient"]
