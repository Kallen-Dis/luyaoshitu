"""模拟新建：按步行距离（而不是直线）判断拟建点能消去哪些盲区方格。"""

import asyncio

import pytest

from app.isochrone.geometry import offset_point
from app.report.simulate import simulate_facility

CENTER = (31.25, 121.42)


def _feature(simulated: bool = False) -> dict:
    cells = []
    for north in (0, 500, 1500):
        lat, lng = offset_point(*CENTER, 0, north)
        cells.append(
            {"lat": lat, "lng": lng, "missing": ["医药"], "unknown": [], "in_circle": True}
        )
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [[]]},
        "properties": {
            "minutes": 15,
            "area_km2": 1.3,
            "compactness": 0.6,
            "center": {"lat": CENTER[0], "lng": CENTER[1]},
            "simulated": simulated,
            "coverage": {"categories": {"医药": 0}, "nearby_categories": {"医药": 1}},
            "blindspots": {
                "basis": "network",
                "layout": "disc",
                "grid_spacing_m": 200,
                "extent_m": 1500,
                "walk_limit_m": 1000,
                "cell_count": 3,
                "blind_count": 3,
                "blind_ratio": {"医药": 1.0},
                "cells": cells,
            },
        },
    }


class FakeClient:
    def __init__(self, walk_m):
        self.walk_m = walk_m
        self.matrix_pairs = 0

    async def walking_matrix_grid(self, origins, destinations):
        self.matrix_pairs += len(origins) * len(destinations)
        return [[{"distance_m": self.walk_m[i], "duration_s": 0.0}] for i in range(len(origins))]


def test_network_verification_only_clears_cells_within_walking_distance():
    # 候选只有直线 0 米与 500 米的两格；500 米那格实际要走 1200 米，不能消去
    client = FakeClient([100.0, 1200.0])
    result = asyncio.run(simulate_facility(client, _feature(), "医药", *CENTER))
    assert result["basis"] == "network"
    assert result["candidate_count"] == 2
    assert result["covered_count"] == 1
    assert result["pairs_used"] == 2
    assert result["after"]["blind_count"] == 2
    assert result["after"]["score"] >= result["before"]["score"]


def test_without_client_it_is_an_upper_bound_estimate():
    result = asyncio.run(simulate_facility(None, _feature(), "医药", *CENTER))
    assert result["basis"] == "estimate"
    assert result["covered_count"] == 2
    assert "上限" in result["approximation"]


def test_simulated_feature_never_calls_the_api():
    client = FakeClient([100.0, 100.0])
    result = asyncio.run(simulate_facility(client, _feature(simulated=True), "医药", *CENTER))
    assert result["basis"] == "estimate"
    assert client.matrix_pairs == 0


def test_feature_without_grid_is_rejected():
    feature = _feature()
    feature["properties"]["blindspots"] = None
    with pytest.raises(ValueError):
        asyncio.run(simulate_facility(None, feature, "医药", *CENTER))


def test_quota_exhaustion_falls_back_to_a_labelled_estimate(tmp_path, monkeypatch):
    """配额用完时改用直线估算并写明原因，而不是显示成「若干格未核验」。"""
    from app.baidu.client import BaiduMapClient
    from app.baidu.errors import QuotaExhaustedError
    from app.config import Settings

    client = BaiduMapClient(Settings(server_ak="test-ak", browser_ak="", cache_dir=tmp_path))

    async def quota(endpoint, params, cost=1.0):
        raise QuotaExhaustedError(302, "天配额超限", endpoint)

    monkeypatch.setattr(client, "_request", quota)
    result = asyncio.run(simulate_facility(client, _feature(), "医药", *CENTER))
    assert result["basis"] == "estimate_quota"
    assert "配额已用尽" in result["approximation"]
    assert result["covered_count"] == 2  # 直线 1 公里内的两格按上限消去
