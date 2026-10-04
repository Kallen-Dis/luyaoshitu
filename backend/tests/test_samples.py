from pathlib import Path

from app.samples import _meta_from, list_samples


def test_list_samples_exposes_contrast_stats():
    """样例列表必须带上对比数字，否则切样例前看不出曹杨与桃浦差在哪。"""
    items = {s.id: s for s in list_samples()}
    assert "isochrone-caoyang-15min" in items
    assert "isochrone-taopu-15min" in items

    cao = items["isochrone-caoyang-15min"]
    tao = items["isochrone-taopu-15min"]
    # 对比要拉得开：曹杨明显好于桃浦。不钉死曹杨的等级：路网层按方格路网基准校准后约 84 分，
    # 离「优」的 85 分只差一点，重新生成时一两分的波动就会跨线，不说明问题
    assert tao.grade == "弱"
    assert cao.grade in ("优", "良")
    assert cao.total is not None and tao.total is not None and cao.total - tao.total >= 30
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
    """处方跟着灰色区域的成因走：桃浦两种成因都有，既要打通也要补设；曹杨全是路网阻隔，只打通。"""
    from app.report.score import build_report
    from app.samples import load_sample

    tao = load_sample("isochrone-taopu-15min")
    tao_report = build_report(
        tao["properties"],
        tao["properties"].get("coverage"),
        tao["properties"].get("blindspots"),
    )
    items = tao_report["prescriptions"]
    tao_actions = {p["action"] for p in items}
    assert {"connect", "site"} <= tao_actions
    # 三类关键设施都有供给缺口，每类至少一处补设
    assert {p["category"] for p in items if p["action"] == "site"} == {
        "生鲜采买",
        "医药",
        "基础教育",
    }
    for p in items:
        assert p["lat"] is not None
        if p["action"] == "connect":
            assert p["target"]["name"] and p["direction"]
            assert "灰色区域 A" in p["title"]
        if p["action"] == "site":
            assert p["basis"] == "estimate" and p["covers"] >= 3

    cao = load_sample("isochrone-caoyang-15min")
    cao_report = build_report(
        cao["properties"],
        cao["properties"].get("coverage"),
        cao["properties"].get("blindspots"),
    )
    cao_actions = {p["action"] for p in cao_report["prescriptions"]}
    assert "connect" in cao_actions
    assert "site" not in cao_actions
