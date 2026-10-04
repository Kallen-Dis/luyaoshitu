"""过街等待校正与施工围挡：等时圈精细化的核心行为。"""

import asyncio

import pytest

from app.baidu.errors import BaiduApiError
from app.isochrone.algorithm import IsochroneConfig, compute_isochrone
from app.isochrone.geometry import haversine_m, offset_point
from app.isochrone.refine import (
    Closure,
    DelayProfile,
    RefineOptions,
    RefineResult,
    build_profile,
    closures_from,
)

CENTER = (31.25, 121.42)
SPEED = 1.17
DETOUR = 1.2


def _straight_route(origin, dest, extra_per_step=60.0, crossing=True):
    """两步直线路线：每步比匀速多 extra_per_step 秒，第一步带「过马路」。"""
    total = haversine_m(*origin, *dest) * DETOUR
    mid = ((origin[0] + dest[0]) / 2, (origin[1] + dest[1]) / 2)
    half = total / 2
    return {
        "distance_m": total,
        "duration_s": total / SPEED + 2 * extra_per_step,
        "steps": [
            {
                "distance_m": half,
                "duration_s": half / SPEED + extra_per_step,
                "turn_type": "过马路左转" if crossing else "直行",
                "instruction": "走一段,过马路左转" if crossing else "直行",
                "path": [list(origin), list(mid)],
            },
            {
                "distance_m": half,
                "duration_s": half / SPEED + extra_per_step,
                "turn_type": "无效",
                "instruction": "到达终点",
                "path": [list(mid), list(dest)],
            },
        ],
    }


class FakeIsoClient:
    """批量算路按「直线 × 1.2 / 1.17 m/s」作答；路线按上面的两步直线路线作答。"""

    def __init__(self, route_ok=True, fail_matrix=False):
        self.route_ok = route_ok
        self.fail_matrix = fail_matrix
        self.matrix_failures: list[str] = []
        self.routes = 0

    async def route_matrix(self, mode_id, origin, destinations):
        if self.fail_matrix:
            self.matrix_failures.append("walk 1x100 status=401")
            return [None] * len(destinations)
        out = []
        for d in destinations:
            dist = haversine_m(*origin, *d) * DETOUR
            out.append({"distance_m": dist, "duration_s": dist / SPEED})
        return out

    async def walking_route(self, origin, destination):
        self.routes += 1
        if not self.route_ok:
            return None
        return _straight_route(origin, destination)


def test_profile_spreads_extra_seconds_along_route():
    route = _straight_route(CENTER, offset_point(*CENTER, 0, 1000))
    prof = build_profile(route, SPEED)
    assert prof.total_delay_s == pytest.approx(120.0)
    # 走到一半（第一步末尾）累计 60 秒
    assert prof.delay_at(prof.length_m / 2) == pytest.approx(60.0)
    assert prof.delay_at(prof.length_m / 4) == pytest.approx(30.0)
    assert prof.crossings_before(prof.length_m / 2) == 1
    assert prof.crossings_before(prof.length_m / 4) == 0
    # 超出路线长度按平均延误率外推，保证耗时随距离单调
    assert prof.delay_at(prof.length_m * 2) == pytest.approx(240.0)


def test_steps_no_slower_than_base_add_no_delay():
    route = _straight_route(CENTER, offset_point(*CENTER, 0, 800), extra_per_step=0.0)
    assert build_profile(route, SPEED).total_delay_s == pytest.approx(0.0)


def test_closure_on_route_sets_blocked_position():
    dest = offset_point(*CENTER, 0, 1000)
    closure_center = offset_point(*CENTER, 0, 400)
    closure = Closure(closure_center[0], closure_center[1], 50.0)
    prof = build_profile(_straight_route(CENTER, dest), SPEED, (closure,))
    assert prof.blocked_at_m is not None
    # 几何上 350 米进入围挡，按路线距离 / 折线长度 = 1.2 折回
    assert prof.blocked_at_m == pytest.approx(350 * DETOUR, rel=0.02)


def test_closure_off_route_is_ignored():
    dest = offset_point(*CENTER, 0, 1000)
    side = offset_point(*CENTER, 90, 400)
    prof = build_profile(_straight_route(CENTER, dest), SPEED, (Closure(*side, 50.0),))
    assert prof.blocked_at_m is None


def test_crossing_delay_shrinks_isochrone():
    cfg = IsochroneConfig(minutes=15, directions=8)
    raw = asyncio.run(compute_isochrone(FakeIsoClient(), CENTER, cfg))
    fine = asyncio.run(compute_isochrone(FakeIsoClient(), CENTER, cfg, RefineOptions(delay=True)))
    # 未校正：d/1.17 = 900 → 直线 877.5 米
    assert raw.rays[0].boundary_m == pytest.approx(877.5, abs=1.0)
    # 校正后到 1000 米点的路线多出 120 秒：t' = d(1/1.17 + 0.1) = 900
    assert fine.rays[0].boundary_m == pytest.approx(785.6, abs=2.0)
    assert fine.area_m2 < raw.area_m2
    assert fine.refine is not None and fine.refine.routes_ok == 8
    props = fine.to_geojson()["properties"]
    assert props["delay"]["applied"] is True
    assert props["delay"]["raw_area_km2"] == pytest.approx(raw.area_m2 / 1e6, rel=1e-3)
    assert props["raw_ring"]
    assert all(r["route"] == "ok" for r in props["rays"])
    assert props["rays"][0]["crossings"] == 1
    # 每个方向都存下路线基线（终点、距离、化简折线），供日后复测巡检对比
    ray = props["rays"][0]
    assert len(ray["route_to"]) == 2 and ray["route_m"] > 0
    assert 2 <= len(ray["route_path"]) <= 3  # 两步直线路线化简后只剩端点与中点
    assert ray["route_path"][0] == [round(CENTER[1], 6), round(CENTER[0], 6)]


def test_route_failure_falls_back_without_failing():
    cfg = IsochroneConfig(minutes=15, directions=8)
    iso = asyncio.run(
        compute_isochrone(FakeIsoClient(route_ok=False), CENTER, cfg, RefineOptions())
    )
    assert iso.rays[0].boundary_m == pytest.approx(877.5, abs=1.0)
    assert all(r.route == "failed" for r in iso.rays)
    assert iso.to_geojson()["properties"]["quality"]["route_failed"] == 8


def test_closure_truncates_only_the_blocked_direction():
    cfg = IsochroneConfig(minutes=15, directions=8, smooth_window=1)
    north = offset_point(*CENTER, 0, 400)
    options = RefineOptions(delay=False, closures=(Closure(north[0], north[1], 50.0),))
    iso = asyncio.run(compute_isochrone(FakeIsoClient(), CENTER, cfg, options))
    assert iso.rays[0].truncated_by_closure
    # 400 米点的路网距离 480 已超过受阻位置 420，边界退回 200 米点
    assert iso.rays[0].boundary_m == pytest.approx(200.0)
    assert not iso.rays[2].truncated_by_closure
    assert iso.rays[2].boundary_m == pytest.approx(877.5, abs=1.0)
    assert iso.to_geojson()["properties"]["quality"]["closure_truncated"] == 1


def test_failed_matrix_batch_refuses_to_draw_a_circle():
    """整批算路失败是「没测到」，不是「走不到」，宁可报错也不出一个缩水的圈。"""
    with pytest.raises(BaiduApiError):
        asyncio.run(compute_isochrone(FakeIsoClient(fail_matrix=True), CENTER))


def test_refine_result_interpolates_between_neighbouring_rays():
    def flat(delay_per_km):
        return DelayProfile(knots_s=[0.0, 1000.0], knots_delay=[0.0, delay_per_km], length_m=1000.0)

    result = RefineResult(
        bearings=[0.0, 90.0, 180.0, 270.0],
        profiles=[flat(100.0), flat(200.0), flat(100.0), None],
        base_speed=SPEED,
        delay_applied=True,
        closures=(),
    )
    delay, blocked, usable = result.adjust(45.0, 500.0)
    assert delay == pytest.approx(75.0)
    assert not blocked and usable
    # 缺剖面的方向只用另一侧
    delay, _, _ = result.adjust(270.0 + 10.0, 500.0)
    assert delay == pytest.approx(50.0)


def test_closures_from_clamps_radius_and_skips_bad_items():
    items = closures_from(
        [{"lat": 31.2, "lng": 121.4, "radius_m": 5000}, {"lat": "x"}, {"lat": 31.2, "lng": 121.4}]
    )
    assert len(items) == 2
    assert items[0].radius_m == 300.0
    assert items[1].radius_m == 50.0
