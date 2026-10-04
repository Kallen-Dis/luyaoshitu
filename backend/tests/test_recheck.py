"""复测巡检：新旧路线比较、分岔定位、疑似点合并，以及没有基线时的明确拒绝。"""

import asyncio

import pytest

from app.isochrone.geometry import haversine_m, offset_point, polyline_length_m
from app.isochrone.recheck import (
    NoBaselineError,
    RecheckConfig,
    baselines,
    compare_ray,
    divergence,
    recheck_routes,
)

CENTER = (31.25, 121.42)


def _north(m):
    return offset_point(*CENTER, 0, m)


def _east_of(p, m):
    return offset_point(*p, 90, m)


def _route(points):
    """把 (lat, lng) 折线包装成 walking_route 的返回格式。"""
    return {
        "distance_m": polyline_length_m(points),
        "duration_s": polyline_length_m(points) / 1.17,
        "steps": [{"path": [list(p) for p in points]}],
    }


STRAIGHT = [CENTER, _north(400), _north(800), _north(1200)]
# 400~800 米那段被封：新路线在 400 米处向东绕 150 米，到 800 米处汇合
DETOUR = [
    CENTER,
    _north(400),
    _east_of(_north(400), 150),
    _east_of(_north(800), 150),
    _north(800),
    _north(1200),
]


def _baseline(points, bearing=0.0):
    return {
        "bearing": bearing,
        "route_to": list(points[-1]),
        "route_m": polyline_length_m(points),
        "route_path": [[lng, lat] for lat, lng in points],
    }


def test_identical_routes_do_not_diverge():
    assert divergence(STRAIGHT, STRAIGHT, 30.0) is None
    out = compare_ray(_baseline(STRAIGHT), _route(STRAIGHT), RecheckConfig())
    assert out["status"] == "same" and "suspect" not in out


def test_detour_locates_the_abandoned_segment():
    span = divergence(STRAIGHT, DETOUR, 30.0)
    assert span is not None
    begin, end = span
    # 旧路线从约 400 米处开始被放弃，约 800 米处重新汇合
    assert begin == pytest.approx(400, abs=40)
    assert end == pytest.approx(800, abs=40)


def test_longer_route_becomes_a_suspect_in_the_middle_of_the_gap():
    out = compare_ray(_baseline(STRAIGHT), _route(DETOUR), RecheckConfig())
    assert out["status"] == "longer"
    assert out["delta_m"] == pytest.approx(300, abs=5)
    s = out["suspect"]
    mid = _north(600)
    assert haversine_m(s["lat"], s["lng"], *mid) < 40
    assert 25 <= s["radius_m"] <= 120
    assert s["precise"] is True
    assert out["old_path"] and out["new_path"]


def test_small_wobble_is_not_reported_as_longer():
    wobble = [CENTER, _north(400), _east_of(_north(600), 20), _north(800), _north(1200)]
    out = compare_ray(_baseline(STRAIGHT), _route(wobble), RecheckConfig())
    assert out["status"] == "same"


def test_shorter_route_is_an_improvement_not_a_suspect():
    out = compare_ray(_baseline(DETOUR), _route(STRAIGHT), RecheckConfig())
    assert out["status"] == "shorter" and "suspect" not in out


def test_failed_fetch_is_marked_failed():
    assert compare_ray(_baseline(STRAIGHT), None, RecheckConfig())["status"] == "failed"


def _feature(rays, **props):
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [[]]},
        "properties": {
            "mode": "walk",
            "center": {"lat": CENTER[0], "lng": CENTER[1]},
            "rays": rays,
            **props,
        },
    }


class FakeRouteClient:
    def __init__(self, answers):
        self.answers = answers  # 终点 → 新路线（None 表示失败）
        self.fresh_calls = 0
        self.route_failures: list[str] = []
        self.requests = 0

    def usage_snapshot(self):
        return {
            "requests": {"route": self.requests},
            "route_failures": len(self.route_failures),
        }

    async def walking_route(self, origin, destination, fresh=False):
        assert fresh, "复测必须跳过缓存"
        self.fresh_calls += 1
        self.requests += 1
        key = (round(destination[0], 6), round(destination[1], 6))
        route = self.answers.get(key)
        if route is None:
            self.route_failures.append("路线规划未返回路线")
        return route


def _key(p):
    return (round(p[0], 6), round(p[1], 6))


def test_recheck_merges_neighbouring_suspects_and_counts_requests():
    # 两个相邻方向都走同一段被封的路：合并成一处疑似
    ray_a = _baseline(STRAIGHT, bearing=0.0)
    other_end = _east_of(_north(1200), 30)
    straight_b = [CENTER, _north(400), _north(800), other_end]
    detour_b = [*DETOUR[:-1], other_end]
    ray_b = _baseline(straight_b, bearing=10.0)
    ray_c = _baseline([CENTER, offset_point(*CENTER, 180, 900)], bearing=180.0)
    client = FakeRouteClient(
        {
            _key(STRAIGHT[-1]): _route(DETOUR),
            _key(other_end): _route(detour_b),
            _key(ray_c["route_to"]): None,
        }
    )
    ray_a["route_fetched_at"] = "2026-09-01T10:00:00+08:00"
    result = asyncio.run(recheck_routes(client, _feature([ray_a, ray_b, ray_c])))
    assert client.fresh_calls == 3 and result["route_requests"] == 3
    assert result["checked"] == 3 and result["longer"] == 2 and result["failed"] == 1
    (suspect,) = result["suspects"]
    assert suspect["id"] == "S1" and sorted(suspect["bearings"]) == [0.0, 10.0]
    assert suspect["label"].startswith("复测疑似S1")
    assert result["baseline"]["fetched_at"].startswith("2026-09-01")
    assert result["route_failures"] == ["路线规划未返回路线"]


def test_no_baseline_is_rejected_with_a_reason():
    with pytest.raises(NoBaselineError, match="没有保存"):
        baselines(_feature([{"bearing": 0, "radius_m": 500}]))
    with pytest.raises(NoBaselineError, match="离线模拟"):
        baselines(_feature([_baseline(STRAIGHT)], simulated=True))
    with pytest.raises(NoBaselineError, match="步行"):
        baselines(_feature([_baseline(STRAIGHT)], mode="drive"))
