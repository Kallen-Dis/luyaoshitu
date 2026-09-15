from app.isochrone.geometry import grid_points, point_in_polygon
from app.poi.catalog import by_name
from app.poi.collect import (
    CategoryResult,
    CleanStats,
    CoverageResult,
    Poi,
    clean,
    counts_within,
    normalize_name,
)

CENTER = (31.248, 121.417)

# 以中心点为圆心的粗略方框，边长约 1.1 公里
SQUARE = [
    (31.243, 121.412),
    (31.253, 121.412),
    (31.253, 121.422),
    (31.243, 121.422),
]


def _record(name: str, lat: float, lng: float) -> dict:
    return {"name": name, "location": {"lat": lat, "lng": lng}, "address": "测试路 1 号"}


def test_normalize_name_strips_branch_suffix_and_fullwidth():
    assert normalize_name("曹杨路药房（兰溪路店）") == "曹杨路药房"
    assert normalize_name("曹杨路药房(兰溪路店)") == "曹杨路药房"
    assert normalize_name(" 曹杨　路药房 ") == "曹杨路药房"


def test_clean_dedups_same_facility_from_multiple_keywords():
    # 「药店」与「药房」两个关键词召回同一家，坐标有几米抖动
    records = [
        _record("国大药房（曹杨店）", 31.2480, 121.4170),
        _record("国大药房(曹杨店)", 31.24802, 121.41701),
    ]
    pois, stats = clean(records, by_name("医药"), CENTER, 1000)
    assert len(pois) == 1
    assert stats.duplicated == 1


def test_clean_rejects_misrecalled_tutoring_as_primary_school():
    records = [
        _record("曹杨第二小学", 31.2485, 121.4175),
        _record("新希望小学生辅导中心", 31.2486, 121.4176),
    ]
    pois, stats = clean(records, by_name("基础教育"), CENTER, 1000)
    assert [p.name for p in pois] == ["曹杨第二小学"]
    assert stats.excluded == 1


def test_clean_drops_invalid_and_out_of_range():
    records = [
        {"name": "无坐标药店", "location": {}},
        _record("零坐标药店", 0, 0),
        _record("远郊药店", 31.40, 121.60),
        _record("圈内药店", 31.2482, 121.4172),
    ]
    pois, stats = clean(records, by_name("医药"), CENTER, 1000)
    assert [p.name for p in pois] == ["圈内药店"]
    assert stats.invalid == 2
    assert stats.out_of_range == 1


def test_clean_uses_polygon_instead_of_circle_when_given():
    # 直线 900 米内，但落在方框之外：圆形半径会放进来，等时圈不会
    outside = _record("圈外药店", 31.2560, 121.4170)
    inside = _record("圈内药店", 31.2482, 121.4172)
    pois, stats = clean([outside, inside], by_name("医药"), CENTER, 1000, polygon=SQUARE)
    assert [p.name for p in pois] == ["圈内药店"]
    assert stats.out_of_range == 1


def test_counts_within_separates_inside_from_search_radius():
    coverage = CoverageResult(center=CENTER, radius_m=2400)
    coverage.results.append(
        CategoryResult(
            category="医药",
            pois=[
                Poi("圈内药店", 31.2482, 121.4172, "医药"),
                Poi("圈外药店", 31.2600, 121.4172, "医药"),
            ],
            stats=CleanStats(),
        )
    )
    assert coverage.categories == {"医药": 2}
    assert counts_within(coverage, SQUARE) == {"医药": 1}


def test_failed_category_is_absent_from_counts_not_zero():
    coverage = CoverageResult(center=CENTER, radius_m=2400, failed=["医药"])
    assert "医药" not in coverage.categories


def test_point_in_polygon_matches_square():
    assert point_in_polygon(31.248, 121.417, SQUARE)
    assert not point_in_polygon(31.258, 121.417, SQUARE)


def test_grid_points_stay_inside_polygon():
    cells = grid_points(SQUARE, 150.0)
    assert len(cells) > 10
    assert all(point_in_polygon(lat, lng, SQUARE) for lat, lng in cells)


def test_grid_spacing_controls_cell_count():
    assert len(grid_points(SQUARE, 300.0)) < len(grid_points(SQUARE, 150.0))
