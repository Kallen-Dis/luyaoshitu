"""按灰色区域成因开方：阻隔 → 打通（方向、目标设施、估算效果），缺口 → 最大覆盖贪心补设。"""

import math

from app.isochrone.geometry import haversine_m
from app.report.prescribe import greedy_sites, prescribe, typical_detour

LAT0, LNG0 = 31.25, 121.42
SPACING = 200.0
D_LAT = SPACING / 111_320
D_LNG = SPACING / (111_320 * math.cos(math.radians(LAT0)))


def cell(ix, iy, missing=("医药",), in_circle=True):
    return {
        "lat": LAT0 + iy * D_LAT,
        "lng": LNG0 + ix * D_LNG,
        "missing": list(missing),
        "unknown": [],
        "nearest_m": {m: 1400.0 for m in missing},
        "in_circle": in_circle,
    }


def grid(cells):
    return {"grid_spacing_m": SPACING, "walk_limit_m": 1000.0, "cells": cells}


def place(name, category, north_m=0.0, east_m=0.0):
    return {
        "category": category,
        "name": name,
        "lat": LAT0 + north_m / 111_320,
        "lng": LNG0 + east_m / (111_320 * math.cos(math.radians(LAT0))),
        "in_circle": False,
    }


PROPS = {"compactness": 0.8, "rays": [{"detour": 1.3}] * 8}


def test_barrier_cells_become_a_connect_order_with_direction_and_target():
    # 一排 5 格，东边直线 100~900 米就有药店，但步行都超过 1 公里：路网阻隔
    cells = [cell(i, 0) for i in range(5)]
    shop = place("河对岸药店", "医药", east_m=900)
    coverage = {"categories": {"医药": 0}, "places": [shop]}
    items = prescribe(PROPS, coverage, grid(cells), blinds=["医药"], failed=[])
    connect = [p for p in items if p["action"] == "connect"]
    assert len(connect) == 1 and not [p for p in items if p["action"] == "site"]
    c = connect[0]
    assert c["direction"] == "东"
    assert c["target"]["name"] == "河对岸药店"
    assert c["region"] == "A" and "灰色区域 A 走得到" in c["title"]
    assert c["cells"] == 5
    # 按典型绕行 1.3 倍估算：直线 ≤ 1000/1.3 ≈ 769 米的格子打通后够得着
    straight = [haversine_m(x["lat"], x["lng"], shop["lat"], shop["lng"]) for x in cells]
    assert c["covers"] == sum(1 for s in straight if s * 1.3 <= 1000) == 4
    assert c["basis"] == "estimate"


def test_supply_gap_gets_greedy_sites_that_do_not_overlap():
    # 两团相距 3 公里的缺口，直线 1 公里内都没有药店：供给缺口，应各补一处
    west = [cell(i, j) for i in range(3) for j in range(3)]
    east = [cell(15 + i, j) for i in range(3) for j in range(3)]
    coverage = {"categories": {"医药": 0}, "places": [place("远处药店", "医药", north_m=5000)]}
    items = prescribe(PROPS, coverage, grid(west + east), blinds=["医药"], failed=[])
    sites = [p for p in items if p["action"] == "site"]
    assert len(sites) == 2
    assert {s["rank"] for s in sites} == {1, 2}
    a, b = sites
    assert haversine_m(a["lat"], a["lng"], b["lat"], b["lng"]) > 2000
    assert all(s["covers"] == 9 for s in sites)


def test_greedy_stops_when_the_gain_is_too_small():
    cells = [cell(0, 0), cell(1, 0)]
    picks = greedy_sites(cells, {0, 1}, radius_m=500)
    assert picks == []  # 两格不到最小收益 3 格，不值得单独建一处


def test_failed_category_gets_no_order_even_with_grid():
    cells = [cell(i, 0) for i in range(5)]
    coverage = {
        "categories": {},
        "failed_categories": ["医药"],
        "places": [place("某店", "生鲜采买", east_m=100)],
    }
    items = prescribe(PROPS, coverage, grid(cells), blinds=[], failed=["医药"])
    assert all(p.get("category") != "医药" for p in items)
    assert items[0]["action"] == "maintain"


def test_typical_detour_is_the_clamped_median():
    assert typical_detour({"rays": [{"detour": 1.1}, {"detour": 1.4}, {"detour": 3.0}]}) == 1.4
    assert typical_detour({"rays": [{"detour": 2.5}] * 3}) == 1.8
    assert typical_detour({}) == 1.35


def test_connect_is_skipped_when_even_a_new_path_would_not_bring_anyone_in():
    # 药店直线 850~950 米：算阻隔，但按 1.3 倍绕行打通后仍超过 1 公里，不开「打通」
    cells = [cell(0, j) for j in range(3)]
    shop = place("远一点的药店", "医药", east_m=900)
    items = prescribe(
        PROPS, {"categories": {"医药": 0}, "places": [shop]}, grid(cells), ["医药"], []
    )
    assert not [p for p in items if p["action"] == "connect"]


def test_connected_barrier_region_split_between_two_schools_still_gets_a_connect():
    """一片连着的阻隔区被两所学校分着挡（各不到门槛），合起来够大，照样开打通处方。"""
    from app.report.prescribe import CONNECT_MIN_CELLS

    # 4 × 1 一排相邻的格子，左两格离 A 校近、右两格离 B 校近。200 米网格门槛是 3 格：
    # 单看哪一校都只有 2 格，够不上；同一片灰色区域合计 4 格，够
    row = [cell(i, 0, ["基础教育"]) for i in range(4)]
    places = [place("A校", "基础教育", north_m=300), place("B校", "基础教育", north_m=300)]
    places[1]["lng"] = row[3]["lng"]
    assert CONNECT_MIN_CELLS == 3
    items = prescribe(
        PROPS,
        {"categories": {"基础教育": 0}, "places": places},
        grid(row),
        blinds=["基础教育"],
        failed=[],
    )
    connect = [p for p in items if p["action"] == "connect"]
    assert len(connect) == 1
    assert connect[0]["cells"] == 2 and connect[0]["region"] == "A"
