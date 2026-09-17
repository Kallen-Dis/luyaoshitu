from app.report.prescribe import prescribe
from app.report.score import build_report


def test_nearby_but_outside_isochrone_prefers_connect_over_new_site():
    """桃浦情形：附近有设施、圈内为零，再盖一家解决不了路网切割。"""
    items = prescribe(
        {"compactness": 0.53},
        {
            "categories": {"生鲜采买": 0, "医药": 0, "基础教育": 0},
            "nearby_categories": {"生鲜采买": 7, "医药": 8, "基础教育": 2},
        },
        {
            "grid_spacing_m": 150,
            "blind_ratio": {"生鲜采买": 0.93},
            "cells": [
                {"lat": 31.28, "lng": 121.37, "missing": ["生鲜采买"]},
                {"lat": 31.2805, "lng": 121.3705, "missing": ["生鲜采买"]},
            ],
        },
        blinds=["生鲜采买", "医药", "基础教育"],
        failed=[],
    )
    actions = [p["action"] for p in items]
    assert "connect" in actions
    assert "site" not in actions
    connect = next(p for p in items if p["category"] == "生鲜采买")
    assert connect["lat"] is not None


def test_existing_coverage_with_local_blind_gets_densify():
    items = prescribe(
        {"compactness": 0.8},
        {"categories": {"基础教育": 2, "医药": 6, "生鲜采买": 5}, "nearby_categories": {}},
        {
            "grid_spacing_m": 150,
            "blind_ratio": {"基础教育": 0.19, "医药": 0.01, "生鲜采买": 0.05},
            "cells": [
                {"lat": 31.25, "lng": 121.42, "missing": ["基础教育"]},
                {"lat": 31.2508, "lng": 121.42, "missing": ["基础教育"]},
                {"lat": 31.2516, "lng": 121.42, "missing": ["基础教育"]},
            ],
        },
        blinds=[],
        failed=[],
    )
    densify = [p for p in items if p["action"] == "densify"]
    assert densify and densify[0]["category"] == "基础教育"
    assert densify[0]["covers"] == 3


def test_failed_search_does_not_get_a_construction_order():
    items = prescribe(
        {"compactness": 0.8},
        {"categories": {"医药": 1}, "nearby_categories": {}},
        None,
        blinds=[],
        failed=["生鲜采买"],
    )
    assert all(p.get("category") != "生鲜采买" for p in items)


def test_healthy_report_still_returns_a_maintain_note():
    items = prescribe(
        {"compactness": 0.9},
        {"categories": {"生鲜采买": 5, "医药": 6, "基础教育": 2}, "nearby_categories": {}},
        {"blind_ratio": {"生鲜采买": 0.05, "医药": 0.01, "基础教育": 0.04}, "cells": []},
        blinds=[],
        failed=[],
    )
    assert items[0]["action"] == "maintain"


def test_build_report_embeds_prescriptions():
    report = build_report(
        {"minutes": 15, "area_km2": 1.3, "compactness": 0.53, "mean_detour": 2.15},
        {"categories": {"生鲜采买": 0}, "nearby_categories": {"生鲜采买": 7}},
    )
    assert report["prescriptions"]
    assert report["prescriptions"][0]["action"] in {"connect", "network"}
