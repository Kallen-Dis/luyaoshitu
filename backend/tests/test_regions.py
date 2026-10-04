"""灰色区域：相邻盲区格合并成片、轮廓正确、编号稳定，并按成因诊断。"""

import math

from app.report.regions import gray_regions
from app.report.score import build_report

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
        "nearest_m": {m: 1300.0 for m in missing},
        "in_circle": in_circle,
    }


def blind(cells):
    return {"grid_spacing_m": SPACING, "walk_limit_m": 1000.0, "cells": cells}


def test_adjacent_cells_merge_and_regions_are_lettered_by_size():
    cells = [cell(0, 0), cell(1, 0), cell(0, 1), cell(1, 1), cell(5, 5), cell(3, 0, missing=())]
    out = gray_regions(blind(cells), {"categories": {"医药": 0}, "places": []})
    assert out["region_count"] == 2
    a, b = out["regions"]
    assert (a["id"], a["cells"]) == ("A", 4)
    assert (b["id"], b["cells"]) == ("B", 1)
    assert a["area_km2"] == 0.16
    # 2×2 方块的外框化简后只剩 4 个拐点
    assert len(a["rings"]) == 1 and len(a["rings"][0]) == 4


def test_l_shape_outline_keeps_six_corners():
    cells = [cell(0, 0), cell(1, 0), cell(0, 1)]
    out = gray_regions(blind(cells), {"categories": {"医药": 0}, "places": []})
    (region,) = out["regions"]
    assert len(region["rings"][0]) == 6


def test_diagonal_touch_is_two_regions():
    """四邻接：只在角上相碰的两格不算连片。"""
    out = gray_regions(blind([cell(0, 0), cell(1, 1)]), {"categories": {}, "places": []})
    assert out["region_count"] == 2


def test_cause_barrier_when_facility_is_straight_nearby():
    """直线 400 米就有药店、步行却超过 1 公里：路网阻隔，处方应是打通。"""
    place = {"category": "医药", "name": "隔河药店", "lat": LAT0 + 400 / 111_320, "lng": LNG0}
    out = gray_regions(blind([cell(0, 0)]), {"categories": {"医药": 0}, "places": [place]})
    (d,) = out["regions"][0]["diagnosis"]
    assert d["cause"] == "barrier" and d["barrier_cells"] == 1
    assert d["nearby"]["place"] == "隔河药店"
    assert d["nearby"]["direction"] == "北"
    assert "路网阻隔" in out["regions"][0]["summary"]


def test_cause_supply_when_nothing_within_straight_limit():
    place = {"category": "医药", "name": "远处药店", "lat": LAT0 + 3000 / 111_320, "lng": LNG0}
    out = gray_regions(blind([cell(0, 0)]), {"categories": {"医药": 0}, "places": [place]})
    assert out["regions"][0]["diagnosis"][0]["cause"] == "supply"


def test_failed_category_cause_is_unknown():
    out = gray_regions(
        blind([cell(0, 0)]),
        {"categories": {}, "places": [], "failed_categories": ["医药"]},
    )
    assert out["regions"][0]["diagnosis"][0]["cause"] == "unknown"


def test_no_grid_means_no_regions_and_report_carries_them():
    assert gray_regions(None, None) is None
    report = build_report(
        {"minutes": 15, "area_km2": 1.2, "compactness": 0.6},
        {"categories": {"医药": 0}, "places": []},
        blind([cell(0, 0), cell(1, 0)]),
    )
    assert report["gray_regions"]["region_count"] == 1


def test_nearby_facilities_without_coordinates_leave_the_cause_unknown():
    """附近有 4 家药店但结果里没有坐标：判不了直线近不近，不能顺手判成供给缺口。"""
    out = gray_regions(
        blind([cell(0, 0)]),
        {"categories": {"医药": 0}, "nearby_categories": {"医药": 4}, "places": []},
    )
    assert out["regions"][0]["diagnosis"][0]["cause"] == "unknown"
