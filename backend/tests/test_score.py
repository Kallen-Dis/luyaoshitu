import math

from app.isochrone.algorithm import RayResult, RaySample, _polygon_at, _solve_boundary
from app.isochrone.geometry import offset_point, polygon_area_m2
from app.report.score import (
    GRID_COMPACTNESS,
    GRID_DETOUR,
    build_report,
    compact_score,
    cover_score,
    detour_score,
    equity_score,
    grade,
    grid_area_km2,
    ideal_area_km2,
    reach_score,
)


def test_reach_score_is_relative_to_grid_diamond():
    # 15 分钟 × 1.2 m/s：半径 1080 米，菱形 2r² ≈ 2.333 km²，直线圆 πr² ≈ 3.664 km²
    assert abs(grid_area_km2(15) - 2.333) < 0.001
    assert abs(grid_area_km2(15) / ideal_area_km2(15) - 2 / math.pi) < 1e-9
    assert reach_score(grid_area_km2(15), 15) == 100
    # 曹杨实测 1.413 km²：占菱形约 61%（占直线圆只有 39%）
    assert 60 <= reach_score(1.413, 15) <= 61
    # 比方格路网还大（斜路、放射路）也封顶
    assert reach_score(3.0, 15) == 100


def test_compact_score_full_marks_at_grid_ratio():
    assert compact_score(GRID_COMPACTNESS) == 100
    assert compact_score(1.0) == 100
    assert compact_score(0.0) == 0
    assert 86 <= compact_score(0.615) <= 88


def test_detour_score_bounds():
    assert detour_score(GRID_DETOUR) == 100
    assert detour_score(1.0) == 100  # 比方格路网还直，封顶
    assert detour_score(2.25) == 0
    assert detour_score(3.0) == 0
    assert detour_score(None) is None


def test_ideal_grid_community_can_reach_top_grade():
    """方格路网 + 设施全部走得到 = 满分。校准前这样的社区只有 80 分，「优」谁都拿不到。"""
    report = build_report(
        {
            "minutes": 15,
            "area_km2": grid_area_km2(15),
            "compactness": GRID_COMPACTNESS,
            "mean_detour": GRID_DETOUR,
        },
        {"categories": {"生鲜采买": 3, "医药": 2, "基础教育": 1}},
        {"blind_ratio": {"生鲜采买": 0.0, "医药": 0.0, "基础教育": 0.0}, "blind_count": 0},
    )
    assert report["total"] == 100
    assert report["grade"] == "优"
    # 直线圆的对比照旧单列：方格路网也只有直线圆的 2/π
    assert abs(report["area_ratio"] - 2 / math.pi) < 0.001
    assert report["grid_area_km2"] == round(grid_area_km2(15), 3)


def test_crossing_waits_still_cost_points():
    """满分假设一路畅通；过街等待缩小的面积照样扣分。曹杨不计等待 1.75 km²、计入后 1.41 km²。"""
    assert reach_score(1.41, 15) < reach_score(1.75, 15)


def test_cover_score_marks_zero_as_blind():
    score, blinds = cover_score({"生鲜采买": 0, "医药": 1, "基础教育": 0})
    assert "生鲜采买" in blinds
    assert "基础教育" in blinds
    assert "医药" not in blinds
    assert score is not None and score < 50


def test_grade_thresholds():
    assert grade(90) == "优"
    assert grade(72) == "良"
    assert grade(60) == "中"
    assert grade(40) == "弱"


def test_build_report_without_coverage_is_pending():
    report = build_report(
        {"minutes": 15, "area_km2": 1.3, "compactness": 0.53, "mean_detour": 2.15}
    )
    assert report["coverage_pending"] is True
    assert report["blinds"] == []
    assert report["total"] > 0


def test_build_report_with_coverage_lists_blinds():
    report = build_report(
        {"minutes": 15, "area_km2": 1.3, "compactness": 0.53, "mean_detour": 2.15},
        {"categories": {"生鲜采买": 0, "医药": 1, "基础教育": 0}, "source": "test"},
    )
    assert report["coverage_pending"] is False
    assert set(report["blinds"]) == {"生鲜采买", "基础教育"}


def test_equity_score_inverts_blind_ratio():
    assert equity_score({"医药": 0.0}) == 100
    assert equity_score({"医药": 1.0}) == 0
    assert equity_score({"医药": 0.2, "基础教育": 0.4}) == 70
    assert equity_score(None) is None


def test_build_report_marks_blindspots_pending_without_grid_data():
    report = build_report({"minutes": 15, "area_km2": 1.3, "compactness": 0.53})
    assert report["blindspots_pending"] is True
    assert report["dimensions"]["equity"] is None


def test_build_report_folds_equity_into_total():
    props = {"minutes": 15, "area_km2": 1.3, "compactness": 0.53, "mean_detour": 2.15}
    coverage = {"categories": {"医药": 2, "基础教育": 1}, "failed_categories": []}
    good = build_report(props, coverage, {"blind_ratio": {"医药": 0.0}, "blind_count": 0})
    bad = build_report(props, coverage, {"blind_ratio": {"医药": 0.9}, "blind_count": 40})
    # 圈内设施数量相同，但多数居民走不到时总分必须更低
    assert good["total"] > bad["total"]
    assert good["blindspots_pending"] is False


def test_failed_category_does_not_count_as_blind():
    report = build_report(
        {"minutes": 15, "area_km2": 1.3, "compactness": 0.53},
        {"categories": {"医药": 1}, "failed_categories": ["生鲜采买"]},
    )
    assert report["blinds"] == []
    assert report["failed_categories"] == ["生鲜采买"]


def test_solve_boundary_interpolates_first_crossing():
    samples = [
        RaySample(200, 0, 0, duration_s=200),
        RaySample(400, 0, 0, duration_s=500),
        RaySample(600, 0, 0, duration_s=1000),
    ]
    radius, barrier, saturated = _solve_boundary(samples, 900)
    assert not barrier and not saturated
    assert 400 < radius < 600


def test_solve_boundary_treats_failure_as_barrier():
    samples = [
        RaySample(200, 0, 0, duration_s=200),
        RaySample(400, 0, 0, duration_s=None),
    ]
    radius, barrier, saturated = _solve_boundary(samples, 900)
    assert barrier and not saturated
    assert radius == 200


def test_offset_north_increases_latitude():
    lat, lng = offset_point(31.28, 121.37, 0, 1113.2)
    assert lat > 31.28
    assert abs(lng - 121.37) < 1e-9


def test_inner_ring_uses_the_same_samples_and_stays_inside():
    """5 分钟圈必须落在 15 分钟圈内侧，且不再发一次算路。"""
    center = (31.25, 121.42)
    rays = []
    for bearing in (0, 90, 180, 270):
        samples = [
            RaySample(200, 0, 0, duration_s=200),
            RaySample(600, 0, 0, duration_s=600),
            RaySample(1200, 0, 0, duration_s=1200),
        ]
        rays.append(
            RayResult(
                bearing_deg=bearing,
                boundary_m=900,
                boundary_lat=0,
                boundary_lng=0,
                samples=samples,
            )
        )
    inner = _polygon_at(center, rays, 300, 1)
    outer = _polygon_at(center, rays, 900, 1)
    # 正北方向：内圈纬度增量应明显小于外圈
    assert inner[0][0] - center[0] < outer[0][0] - center[0]


def test_square_area_is_positive():
    square = [(0, 0), (0, 0.01), (0.01, 0.01), (0.01, 0)]
    assert polygon_area_m2(square) > 0
