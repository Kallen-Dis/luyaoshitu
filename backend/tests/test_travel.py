import pytest

from app.travel import DRIVE_FREE, DRIVE_TRAFFIC, RIDE, WALK, get_mode


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError, match="未知出行方式"):
        get_mode("teleport")


def test_drive_traffic_is_the_only_mode_that_claims_live_congestion():
    """路况只应出现在明确选择的驾车策略上，不能让步行圈假装考虑拥堵。"""
    assert not WALK.uses_traffic
    assert not RIDE.uses_traffic
    assert not DRIVE_FREE.uses_traffic
    assert DRIVE_TRAFFIC.uses_traffic
    assert DRIVE_FREE.extra_params["tactics"] == 13
    assert DRIVE_TRAFFIC.extra_params["tactics"] == 11


def test_sampling_radius_grows_with_speed():
    """驾车若仍用步行的 1400 米上界，15 分钟圈会整圈饱和，形状失去意义。"""
    assert max(WALK.radii_m) < max(RIDE.radii_m) < max(DRIVE_FREE.radii_m)
    assert WALK.allow_grid_blindspots
    assert not RIDE.allow_grid_blindspots


def test_route_matrix_hits_riding_and_driving_endpoints(tmp_path, monkeypatch):
    """出行方式必须打到对应接口，且驾车路况策略不能跟步行缓存混用。"""
    import asyncio

    from app.baidu.client import BaiduMapClient
    from app.config import Settings

    client = BaiduMapClient(
        Settings(server_ak="test-ak", browser_ak="", cache_dir=tmp_path, matrix_batch_size=100)
    )
    seen: list[tuple] = []

    async def fake_request(endpoint, params, cost=1.0):
        seen.append((endpoint, params.get("riding_type"), params.get("tactics")))
        return {"result": [{"distance": {"value": 1}, "duration": {"value": 1}}]}

    monkeypatch.setattr(client, "_request", fake_request)
    origin, dest = (31.24, 121.41), [(31.25, 121.42)]
    asyncio.run(client.route_matrix("ride", origin, dest))
    asyncio.run(client.route_matrix("drive_traffic", origin, dest))
    asyncio.run(client.route_matrix("walk", origin, dest))

    assert seen[0][0] == "/routematrix/v2/riding"
    assert seen[0][1] == 0
    assert seen[1][0] == "/routematrix/v2/driving"
    assert seen[1][2] == 11
    assert seen[2][0] == "/routematrix/v2/walking"
