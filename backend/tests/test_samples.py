from pathlib import Path

from app.samples import _meta_from, list_samples


def test_list_samples_exposes_contrast_stats():
    """样例列表必须带上对比数字，否则切样例前看不出曹杨与桃浦差在哪。"""
    items = {s.id: s for s in list_samples()}
    assert "isochrone-caoyang-15min" in items
    assert "isochrone-taopu-15min" in items

    cao = items["isochrone-caoyang-15min"]
    tao = items["isochrone-taopu-15min"]
    assert cao.grade == "良"
    assert tao.grade == "弱"
    assert cao.area_ratio is not None and cao.area_ratio < 0.6
    assert tao.area_ratio is not None and tao.area_ratio < cao.area_ratio
    assert cao.facilities_in and cao.facilities_in > 0
    assert tao.facilities_in == 0
    assert tao.facilities_nearby and tao.facilities_nearby > 0


def test_meta_survives_snapshot_without_report(tmp_path: Path):
    payload = {
        "properties": {
            "name": "空快照",
            "minutes": 15,
            "center": {"lat": 31.2, "lng": 121.4},
            "area_km2": 1.0,
            "mean_radius_m": 600,
            "compactness": 0.5,
        }
    }
    meta = _meta_from(tmp_path / "isochrone-empty.geojson", payload)
    assert meta.grade is None
    assert meta.facilities_in is None
