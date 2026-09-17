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


def test_snapshots_open_into_actionable_prescriptions():
    """加载快照后必须能开方，且桃浦优先打通、建议点落在网格范围内。"""
    from app.report.score import build_report
    from app.samples import load_sample

    tao = load_sample("isochrone-taopu-15min")
    tao_report = build_report(
        tao["properties"],
        tao["properties"].get("coverage"),
        tao["properties"].get("blindspots"),
    )
    tao_actions = {p["action"] for p in tao_report["prescriptions"]}
    assert "connect" in tao_actions
    assert "site" not in tao_actions
    located = [p for p in tao_report["prescriptions"] if p["lat"] is not None]
    assert located

    cao = load_sample("isochrone-caoyang-15min")
    cao_report = build_report(
        cao["properties"],
        cao["properties"].get("coverage"),
        cao["properties"].get("blindspots"),
    )
    assert cao_report["prescriptions"]
    densify_or_connect = {p["action"] for p in cao_report["prescriptions"]}
    assert densify_or_connect & {"densify", "network", "maintain", "connect"}
