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


def test_caoyang_borderline_route_does_not_excavate_lanhuayuan():
    from app.isochrone.fusion import field_time
    from app.isochrone.geometry import point_in_polygon
    from app.samples import load_sample

    feature = load_sample("isochrone-caoyang-15min")
    fusion = feature["properties"]["fusion"]
    # 用户截图中的兰花园内部圈；几何、热力都必须修正，不能只隐藏内边线。
    lat, lng = 31.24303829, 121.41414814
    assert point_in_polygon(lat, lng, feature["geometry"])
    assert field_time(fusion["time_field"], lat, lng) < 900
    assert fusion["quality"]["holes"] == 0
    observed = next(
        p
        for p in fusion["measured_points"]
        if abs(p["lat"] - 31.243155) < 1e-6 and abs(p["lng"] - 121.413970) < 1e-6
    )
    assert observed["time_s"] == 902  # 留下真实记录，而非改数据使孔洞消失。


def test_default_samples_preserve_published_fusion_geometry_and_statistics():
    from app.isochrone.geometry import polygon_parts
    from app.main import _load_sample_or_404
    from app.samples import load_sample

    for sample, method in (("caoyang", "fusion"), ("taopu", "fusion")):
        sample_id = f"isochrone-{sample}-15min"
        stored = load_sample(sample_id)
        opened = _load_sample_or_404(sample_id)
        props = opened["properties"]
        assert props["geometry_method"] == method
        assert opened["geometry"] == stored["geometry"]
        assert props["area_km2"] == stored["properties"]["area_km2"]
        assert props["coverage"]["categories"] == stored["properties"]["coverage"]["categories"]
        assert props["report"]["total"] == stored["properties"]["report"]["total"]
        assert all(len(face) == 1 for face in polygon_parts(opened["geometry"]))
        assert ("fusion" in props) == (method == "fusion")
        assert props["blindspots"]["scope_method"] == "fusion"
        if sample == "taopu":
            assert props["fusion"]["quality"]["validation_status"] == "estimated"
            assert props["fusion"]["quality"]["path_violations"] == 0


def test_snapshots_open_into_actionable_prescriptions():
    """新范围改变分组时必须重算处方；不能沿用原射线结果中的打通建议。"""
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
    assert cao_actions == {"maintain"}
    assert "site" not in cao_actions
    assert cao["properties"]["blindspots"]["blind_count"] > 0
    assert (
        cao["properties"]["fusion"]["ray_statistics"]["blind_count"]
        > cao["properties"]["blindspots"]["blind_count"]
    )
    assert "盲区" in cao_report["prescriptions"][0]["reason"]
