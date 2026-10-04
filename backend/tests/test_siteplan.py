"""核验选址：备选点彼此错开、按实测效果重排、受点对预算约束、给最佳一处写地址。"""

import asyncio
import math

import pytest

from app.isochrone.geometry import haversine_m
from app.report.siteplan import plan_site, site_candidates

LAT0, LNG0 = 31.25, 121.42
SPACING = 200.0
D_LAT = SPACING / 111_320
D_LNG = SPACING / (111_320 * math.cos(math.radians(LAT0)))


def cell(ix, iy):
    return {
        "lat": LAT0 + iy * D_LAT,
        "lng": LNG0 + ix * D_LNG,
        "missing": ["医药"],
        "unknown": [],
        "nearest_m": {"医药": 1800.0},
        "in_circle": True,
    }


def _feature(cells, simulated=False):
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [[]]},
        "properties": {
            "minutes": 15,
            "area_km2": 1.2,
            "compactness": 0.7,
            "simulated": simulated,
            "rays": [{"detour": 1.3}] * 8,
            "coverage": {
                "categories": {"医药": 0},
                "places": [
                    {"category": "医药", "name": "远处药店", "lat": LAT0 + 0.05, "lng": LNG0}
                ],
            },
            "blindspots": {
                "grid_spacing_m": SPACING,
                "walk_limit_m": 1000.0,
                "cell_count": len(cells),
                "blind_count": len(cells),
                "blind_ratio": {"医药": 1.0},
                "cells": cells,
            },
        },
    }


class FakeClient:
    """步行距离 = 直线 × factor；东半边（ix ≥ 3）被一条河挡住，步行翻倍。"""

    def __init__(self, factor=1.2):
        self.factor = factor
        self.matrix_pairs = 0
        self.regeo = 0

    async def walking_matrix_grid(self, origins, destinations):
        self.matrix_pairs += len(origins) * len(destinations)
        (dlat, dlng) = destinations[0]
        out = []
        for lat, lng in origins:
            d = haversine_m(lat, lng, dlat, dlng) * self.factor
            if (lng - LNG0) / D_LNG >= 2.5 or (dlng - LNG0) / D_LNG >= 2.5:
                d *= 2
            out.append([{"distance_m": d, "duration_s": d / 1.2}])
        return out

    async def reverse_geocode(self, lat, lng):
        self.regeo += 1
        return {"address": "某区某路 1 号", "description": "某小区附近", "street": "某路"}


CELLS = [cell(i, j) for i in range(6) for j in range(3)]


def test_alternatives_are_spread_out():
    picks, basis = site_candidates(_feature(CELLS), "医药", 3)
    assert basis["demand_cells"] == len(CELLS)
    for a in picks:
        for b in picks:
            if a is not b:
                assert haversine_m(a["lat"], a["lng"], b["lat"], b["lng"]) >= 300


def test_verification_reranks_by_measured_coverage_and_adds_an_address():
    client = FakeClient()
    result = asyncio.run(plan_site(client, _feature(CELLS), "医药", alternatives=3))
    assert result["verified"] is True
    verified = [c for c in result["candidates"] if c["status"] == "verified"]
    assert verified
    best = result["best"]
    assert best["verified"] == max(c["verified"] for c in verified)
    # 河东的点步行翻倍，实测不会是最好的
    assert (best["lng"] - LNG0) / D_LNG < 2.5
    assert best["place"]["address"] == "某区某路 1 号"
    assert best["simulation"]["basis"] == "network"
    assert result["pairs_used"] == client.matrix_pairs > 0
    assert client.regeo == 1


def test_budget_stops_further_verification():
    client = FakeClient()
    result = asyncio.run(
        plan_site(client, _feature(CELLS), "医药", alternatives=3, budget_pairs=20)
    )
    statuses = [c["status"] for c in result["candidates"]]
    assert "over_budget" in statuses
    assert result["pairs_used"] <= 20


def test_simulated_feature_only_estimates():
    result = asyncio.run(plan_site(None, _feature(CELLS, simulated=True), "医药"))
    assert result["verified"] is False
    assert all(c["status"] == "estimated" for c in result["candidates"])
    assert "place" not in result["best"]


def test_category_without_supply_gap_is_refused():
    feature = _feature(CELLS)
    # 药店就在网格旁边：缺口全是路网阻隔，不该补设
    feature["properties"]["coverage"]["places"][0].update(lat=LAT0, lng=LNG0 + 3 * D_LNG)
    with pytest.raises(ValueError, match="打通"):
        asyncio.run(plan_site(FakeClient(), feature, "医药"))


def test_default_budget_verifies_three_alternatives_on_the_shipped_samples():
    """100 米网格上每个备选约百个点对：默认预算要够前 3 个备选全部核验，不能第 3 个就超预算。"""
    import json
    from pathlib import Path

    from app.report.siteplan import DEFAULT_BUDGET_PAIRS

    samples = Path(__file__).resolve().parents[2] / "data" / "samples"
    checked = 0
    for path in sorted(samples.glob("isochrone-*-15min.geojson")):
        feature = json.loads(path.read_text(encoding="utf-8"))
        cells = feature["properties"].get("blindspots", {}).get("cells") or []
        for category in ("生鲜采买", "医药", "基础教育"):
            try:
                picks, _ = site_candidates(feature, category, 3)
            except ValueError:
                continue
            need = sum(
                1
                for p in picks
                for c in cells
                if category in (c.get("missing") or [])
                and haversine_m(c["lat"], c["lng"], p["lat"], p["lng"]) <= 1000.0
            )
            assert need <= DEFAULT_BUDGET_PAIRS, (path.name, category, need)
            checked += 1
    assert checked > 0
