"""标注校验：几何、文本清洗、有效期、可改字段。"""

import math

import pytest

from app.markings import models
from app.markings.models import MarkingValidationError, normalize

LAT0, LNG0 = 31.25, 121.42
D_LAT = 1 / 111_320
D_LNG = 1 / (111_320 * math.cos(math.radians(LAT0)))


def square(side_m=100.0, x0=0.0, y0=0.0):
    """[经度, 纬度] 的正方形顶点（逆时针）。"""
    pts = [(x0, y0), (x0 + side_m, y0), (x0 + side_m, y0 + side_m), (x0, y0 + side_m)]
    return [[LNG0 + x * D_LNG, LAT0 + y * D_LAT] for x, y in pts]


def test_closure_defaults_and_bbox():
    spec, cols = normalize({"type": "closure", "lat": LAT0, "lng": LNG0, "radius_m": 60})
    assert spec["kind"] == "construction" and spec["radius_m"] == 60
    assert cols["min_lat"] < LAT0 < cols["max_lat"]
    assert (cols["max_lat"] - cols["min_lat"]) * 111_320 == pytest.approx(120, abs=0.5)
    assert models.ttl_days(spec) == 60


def test_closure_radius_and_location_are_checked():
    with pytest.raises(MarkingValidationError, match="半径"):
        normalize({"type": "closure", "lat": LAT0, "lng": LNG0, "radius_m": 500})
    with pytest.raises(MarkingValidationError, match="中国"):
        normalize({"type": "closure", "lat": 121.42, "lng": 31.25, "radius_m": 50})
    with pytest.raises(MarkingValidationError, match="有效数字"):
        normalize({"type": "closure", "lat": float("nan"), "lng": LNG0})


def test_facility_types_require_category_name_and_reason():
    with pytest.raises(MarkingValidationError, match="类别"):
        normalize({"type": "facility_extra", "lat": LAT0, "lng": LNG0, "name": "卫生室"})
    with pytest.raises(MarkingValidationError, match="名称"):
        normalize({"type": "facility_extra", "lat": LAT0, "lng": LNG0, "category": "医药"})
    with pytest.raises(MarkingValidationError, match="失效原因"):
        normalize(
            {"type": "facility_missing", "lat": LAT0, "lng": LNG0, "category": "医药", "name": "x"}
        )
    spec, cols = normalize(
        {
            "type": "facility_missing",
            "lat": LAT0,
            "lng": LNG0,
            "category": "医药",
            "name": "  幸福  药房 ",
            "reason": "closed",
        }
    )
    assert spec["name"] == "幸福 药房" and cols["category"] == "医药"


def test_text_cleaning_removes_links_and_phone_numbers():
    text = models.clean_text("围挡 见 https://x.cn/a 电话13812345678 或 021-62345678", 60, "说明")
    assert "http" not in text and "138" not in text and "021" not in text
    assert "［链接已移除］" in text and "［号码已移除］" in text
    with pytest.raises(MarkingValidationError, match="最多 60"):
        models.clean_text("字" * 61, 60, "说明")
    assert models.clean_text("a\u200bb\x00c", 60, "说明") == "abc"


def test_gray_area_polygon_is_validated():
    spec, cols = normalize(
        {"type": "gray_area", "polygon": square(), "reason": "capacity", "categories": ["基础教育"]}
    )
    assert len(spec["polygon"]) == 4
    assert cols["geometry"]["coordinates"][0][0] == cols["geometry"]["coordinates"][0][-1]
    # 代表点落在区域里
    assert cols["min_lat"] < cols["lat"] < cols["max_lat"]
    assert models.distance_to(spec, cols["lat"], cols["lng"]) == 0

    closed = [*square(), square()[0]]
    spec2, _ = normalize(
        {"type": "gray_area", "polygon": closed, "reason": "capacity", "categories": ["医药"]}
    )
    assert len(spec2["polygon"]) == 4  # 闭合点被去掉


@pytest.mark.parametrize(
    ("polygon", "message"),
    [
        (square()[:2], "至少需要 3 个"),
        # 蝴蝶结：两条边交叉
        ([square()[0], square()[2], square()[1], square()[3]], "自相交"),
        (square(side_m=10), "太小"),
        (square(side_m=1200), "超过 1 平方公里"),
    ],
)
def test_bad_polygons_are_rejected(polygon, message):
    with pytest.raises(MarkingValidationError, match=message):
        normalize(
            {"type": "gray_area", "polygon": polygon, "reason": "capacity", "categories": ["医药"]}
        )


def test_gray_area_other_reason_needs_a_note_and_categories_are_ordered():
    with pytest.raises(MarkingValidationError, match="说明"):
        normalize(
            {"type": "gray_area", "polygon": square(), "reason": "other", "categories": ["医药"]}
        )
    spec, _ = normalize(
        {
            "type": "gray_area",
            "polygon": square(),
            "reason": "other",
            "note": "只有早上开",
            "categories": ["基础教育", "生鲜采买", "基础教育"],
        }
    )
    assert spec["categories"] == ["生鲜采买", "基础教育"]


def test_ttl_and_patch_rules():
    spec, _ = normalize({"type": "closure", "lat": LAT0, "lng": LNG0, "kind": "gated"})
    assert models.ttl_days(spec) == 365
    assert models.ttl_days(spec, 30) == 30
    with pytest.raises(MarkingValidationError):
        models.ttl_days(spec, 3)
    with pytest.raises(MarkingValidationError):
        models.ttl_days(spec, 10.5)
    merged = models.merge_patch(spec, {"radius_m": 80, "note": None})
    assert merged["radius_m"] == 80 and merged["kind"] == "gated"
    with pytest.raises(MarkingValidationError, match="不能修改"):
        models.merge_patch(spec, {"category": "医药"})


def test_titles_are_readable():
    spec, _ = normalize({"type": "closure", "lat": LAT0, "lng": LNG0, "radius_m": 60})
    assert models.title(spec) == "施工围挡 · 半径 60 米"
    spec, _ = normalize(
        {"type": "facility_extra", "lat": LAT0, "lng": LNG0, "category": "医药", "name": "村卫生室"}
    )
    assert models.title(spec) == "补录医药 · 村卫生室"
