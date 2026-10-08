"""分析叠加：挑哪些标注、围挡合并、设施修正、逐条影响，以及整条流水线的 A/B 两次判定。"""

import asyncio
import math

from app.isochrone.geometry import haversine_m, offset_point
from app.isochrone.refine import Closure
from app.main import AnalysisState, IsochroneRequest, _analysis_steps
from app.markings import apply
from app.poi.collect import CategoryResult, CleanStats, CoverageResult, Poi
from app.travel import WALK

LAT0, LNG0 = 31.25, 121.42


def mk(mid, typ, spec, *, status="pending", mine=False, confidence=0.5, distance=100):
    return {
        "id": mid,
        "version": 1,
        "type": typ,
        "title": f"#{mid}",
        "status": status,
        "mine": mine,
        "source": "user",
        "confidence": confidence,
        "disputed": False,
        "confirms": 0,
        "disputes": 0,
        "photo_count": 0,
        "distance_m": distance,
        "spec": {"type": typ, **spec},
    }


def closure_spec(north_m=0.0, radius=50.0):
    lat, lng = offset_point(LAT0, LNG0, 0, north_m)
    return {"lat": lat, "lng": lng, "radius_m": radius, "kind": "construction"}


def test_auto_uses_verified_and_own_and_suggests_the_rest():
    nearby = [
        mk(1, "closure", closure_spec(100), status="verified"),
        mk(2, "closure", closure_spec(300), mine=True),
        mk(3, "closure", closure_spec(500)),
        mk(4, "closure", closure_spec(700)),
    ]
    p = apply.plan(nearby, "auto", include=[3], exclude=[1])
    assert sorted(m["id"] for m in p.applied) == [2, 3]
    assert [m["id"] for m in p.suggested] == [4]
    assert p.skipped[0]["id"] == 1 and "不使用" in p.skipped[0]["reason"]
    assert [m["id"] for m in apply.plan(nearby, "all").applied] == [2, 1, 3, 4]
    none = apply.plan(nearby, "none")
    assert none.applied == [] and len(none.suggested) == 4


def test_non_walking_skips_closures_and_caps_facility_markings():
    nearby = [mk(1, "closure", closure_spec(), status="verified")] + [
        mk(
            10 + i,
            "facility_extra",
            {"lat": LAT0, "lng": LNG0, "category": "医药", "name": f"店{i}"},
            status="verified",
        )
        for i in range(12)
    ]
    p = apply.plan(nearby, "auto", walking=False)
    assert all(m["type"] == "facility_extra" for m in p.applied) and len(p.applied) == 10
    reasons = {s["id"]: s["reason"] for s in p.skipped}
    assert "步行" in reasons[1] and sum("单次最多" in r for r in reasons.values()) == 2


def test_merge_closures_dedupes_and_caps():
    request = (Closure(LAT0, LNG0, 50.0),)
    nearby = [
        mk(1, "closure", closure_spec(5, 50), mine=True),  # 与临时围挡重合
        mk(2, "closure", closure_spec(400, 60), status="verified"),
    ]
    p = apply.plan(nearby, "auto")
    merged, by_id = apply.merge_closures(request, p)
    assert len(merged) == 2 and list(by_id) == [2]
    assert p.skipped[0]["id"] == 1 and "重合" in p.skipped[0]["reason"]


def _coverage(pois_by_cat, failed=()):
    return CoverageResult(
        center=(LAT0, LNG0),
        radius_m=2500,
        results=[CategoryResult(c, list(p), CleanStats()) for c, p in pois_by_cat.items()],
        failed=list(failed),
    )


def test_modify_coverage_removes_adds_and_skips():
    shop = Poi("幸福药房", *offset_point(LAT0, LNG0, 90, 300), "医药")
    school = Poi("实验小学", *offset_point(LAT0, LNG0, 0, 600), "基础教育")
    cov = _coverage({"医药": [shop], "基础教育": [school]}, failed=["生鲜采买"])
    nearby = [
        mk(
            1,
            "facility_missing",
            {
                "lat": shop.lat + 0.0002,
                "lng": shop.lng,
                "category": "医药",
                "name": "幸福药房（东店）",
                "reason": "closed",
            },
            mine=True,
        ),
        mk(
            2,
            "facility_missing",
            {
                "lat": LAT0,
                "lng": LNG0,
                "category": "医药",
                "name": "不存在的店",
                "reason": "closed",
            },
            mine=True,
        ),
        mk(
            3,
            "facility_extra",
            {
                "lat": school.lat,
                "lng": school.lng + 0.0001,
                "category": "基础教育",
                "name": "实验小学",
            },
            mine=True,
        ),
        mk(
            4,
            "facility_extra",
            {"lat": LAT0, "lng": LNG0, "category": "基础教育", "name": "村小"},
            mine=True,
        ),
        mk(
            5,
            "facility_extra",
            {"lat": LAT0, "lng": LNG0, "category": "生鲜采买", "name": "早市"},
            mine=True,
        ),
    ]
    p = apply.plan(nearby, "auto")
    out = apply.modify_coverage(cov, p)
    assert out.pois_of("医药") == [] and cov.pois_of("医药") == [shop]  # 原对象不变
    names = [x.name for x in out.pois_of("基础教育")]
    assert names == ["实验小学", "村小"]
    added = out.pois_of("基础教育")[1]
    assert added.source == "user" and added.marking_id == 4
    reasons = {s["id"]: s["reason"] for s in p.skipped}
    assert "已收录" in reasons[3] and "检索失败" in reasons[5]
    assert sorted(m["id"] for m in p.applied) == [1, 2, 4]
    assert p.effects[2]["unmatched_exclusion"] == "不存在的店"
    places = out.as_dict()["places"]
    assert any(pl.get("source") == "user" and pl["marking_id"] == 4 for pl in places)


def test_route_hits_use_saved_route_baselines():
    north = [offset_point(LAT0, LNG0, 0, d) for d in (0, 400, 800)]
    east = [offset_point(LAT0, LNG0, 90, d) for d in (0, 400, 800)]
    rays = [
        {"bearing": 0.0, "route_path": [[lng, lat] for lat, lng in north]},
        {"bearing": 90.0, "route_path": [[lng, lat] for lat, lng in east]},
    ]
    assert apply.route_hits(closure_spec(400, 30), rays) == [0.0]


# ---------- 整条流水线 ----------


class FakePipelineClient:
    """回答分析流水线要用的全部接口：步行距离 = 直线 × 1.25，一条两段的直线路线。"""

    def __init__(self, pois):
        self.pois = pois  # 关键词 → [(名称, 纬度, 经度)]
        self.quota_events: list = []
        self.traffic_ages: list = []
        self.traffic_stale: list = []
        self.traffic_stale_reason = None
        self.matrix_failures: list = []
        self.route_failures: list = []
        self.pairs = 0

    @staticmethod
    def _walk(a, b):
        d = haversine_m(*a, *b) * 1.25
        return {"distance_m": d, "duration_s": d / 1.2}

    async def route_matrix(self, mode_id, origin, destinations):
        self.pairs += len(destinations)
        return [self._walk(origin, d) for d in destinations]

    async def walking_matrix(self, origin, destinations):
        return await self.route_matrix("walk", origin, destinations)

    async def walking_matrix_grid(self, origins, destinations):
        self.pairs += len(origins) * len(destinations)
        return [[self._walk(o, d) for d in destinations] for o in origins]

    async def walking_route(self, origin, destination, fresh=False):
        mid = ((origin[0] + destination[0]) / 2, (origin[1] + destination[1]) / 2)
        total = haversine_m(*origin, *destination) * 1.25
        return {
            "distance_m": total,
            "duration_s": total / 1.2,
            "steps": [
                {
                    "distance_m": total / 2,
                    "duration_s": total / 2.4,
                    "turn_type": "",
                    "instruction": "",
                    "path": [list(origin), list(mid)],
                },
                {
                    "distance_m": total / 2,
                    "duration_s": total / 2.4,
                    "turn_type": "",
                    "instruction": "",
                    "path": [list(mid), list(destination)],
                },
            ],
        }

    async def search_poi_all(self, keyword, lat, lng, radius, page_size=20, max_pages=3, scope=1):
        return [
            {"name": n, "location": {"lat": la, "lng": ln}, "address": ""}
            for n, la, ln in self.pois.get(keyword, [])
        ]

    async def search_poi(self, keyword, lat, lng, radius, page_size=20, page_num=0, scope=1):
        # 按校名查校门：假客户端没有子点数据，学校照旧按坐标点测
        return []


def _run(client, req, device_hash=None, nearby=None, monkeypatch=None):
    async def fake_nearby(lat, lng, radius, device):
        return list(nearby or [])

    monkeypatch.setattr("app.main.markings_for_analysis", fake_nearby)

    async def go():
        feature = None
        stages = []
        async for event, data in _analysis_steps(
            client, req, WALK, (LAT0, LNG0), AnalysisState(), device_hash
        ):
            if event == "stage":
                stages.append(data["stage"])
            if "feature" in data:
                feature = data["feature"]
        return feature, stages

    return asyncio.run(go())


def _req(**kw):
    return IsochroneRequest(
        lat=LAT0,
        lng=LNG0,
        directions=8,
        grid_spacing_m=400,
        grid_extent_m=800,
        crossing_delay=True,
        **kw,
    )


def test_pipeline_without_markings_is_unchanged(monkeypatch):
    far = offset_point(LAT0, LNG0, 90, 2400)
    client = FakePipelineClient({"药店": [("远处药店", *far)]})
    feature, stages = _run(client, _req(), monkeypatch=monkeypatch)
    props = feature["properties"]
    assert "markings" not in stages
    assert props["markings"]["applied"] == [] and props["markings"]["effect"] is None
    assert props["markings"]["query_radius_m"] == 1800


def test_searched_address_becomes_the_result_name(monkeypatch):
    """按地址体检时把输入的地址写进结果，界面不再显示「当前地点」；不传就不写。"""
    client = FakePipelineClient({})
    named, _ = _run(client, _req(name="  上海市普陀区曹杨新村街道 "), monkeypatch=monkeypatch)
    assert named["properties"]["name"] == "上海市普陀区曹杨新村街道"
    plain, _ = _run(client, _req(), monkeypatch=monkeypatch)
    assert "name" not in plain["properties"]


def test_pipeline_with_an_extra_pharmacy_reports_both_scores(monkeypatch):
    # 菜场和小学就在中心，只缺药店：补录一处药房后，所有方格都不再是盲区
    far = offset_point(LAT0, LNG0, 90, 2400)
    client = FakePipelineClient(
        {
            "药店": [("远处药店", *far)],
            "菜市场": [("中心菜场", LAT0, LNG0)],
            "小学": [("中心小学", *offset_point(LAT0, LNG0, 180, 100))],
        }
    )
    extra = mk(
        7,
        "facility_extra",
        {"lat": LAT0, "lng": LNG0, "category": "医药", "name": "村卫生室药房"},
        status="verified",
    )
    feature, stages = _run(client, _req(), nearby=[extra], monkeypatch=monkeypatch)
    props = feature["properties"]
    assert "markings" in stages
    m = props["markings"]
    assert [a["id"] for a in m["applied"]] == [7]
    assert "不再缺医药" in m["applied"][0]["effect"]
    assert m["effect"]["score_with_markings"] >= m["effect"]["score_algorithm"]
    assert m["effect"]["blind_cells_delta"] < 0
    before = props["markings"]["baseline"]["blind_cells"]
    assert props["report"]["blind_cell_count"] < before
    assert any(p.get("source") == "user" for p in props["coverage"]["places"])
    # 前端切到「纯算法」要用的差异：变了的格子在 A 里缺医药、在 B 里不缺
    base = m["baseline"]
    changed = base["cells_changed"]
    assert changed and all("医药" in c["missing"] for c in changed)
    b_cells = {(round(c["lat"], 5), round(c["lng"], 5)): c for c in props["blindspots"]["cells"]}
    assert all(
        "医药" not in b_cells[(round(c["lat"], 5), round(c["lng"], 5))]["missing"] for c in changed
    )
    restored = len(
        {(round(c["lat"], 5), round(c["lng"], 5)) for c in changed}
        | {k for k, c in b_cells.items() if c["missing"]}
    )
    assert restored == before
    assert base["places"] is not None
    assert not any(p.get("source") == "user" for p in base["places"])
    assert base["regions"] is not None and base["ring"] is None and base["inner_rings"] is None


def test_pipeline_with_a_shared_closure_keeps_the_algorithm_ring(monkeypatch):
    client = FakePipelineClient({})
    block = mk(9, "closure", closure_spec(200, 40), status="verified")
    feature, _ = _run(client, _req(), nearby=[block], monkeypatch=monkeypatch)
    props = feature["properties"]
    m = props["markings"]
    assert "挡住 1 个方向" in m["applied"][0]["effect"]
    assert props["closures"] == []  # 共享围挡不写进本次的临时围挡
    assert m["baseline"]["ring"] and m["baseline"]["area_km2"] > props["area_km2"]
    # 围挡改了圈：纯算法的内圈与设施的圈内归属一并带上，供前端切换
    inner = m["baseline"]["inner_rings"]
    assert (inner is None) == (props.get("rings") is None)
    if inner is not None:
        assert [r["minutes"] for r in inner] == [r["minutes"] for r in props["rings"]]
    assert m["baseline"]["places"] is not None
    assert m["effect"]["area_delta_km2"] < 0
    assert math.isclose(
        m["baseline"]["area_km2"] + m["effect"]["area_delta_km2"], props["area_km2"], abs_tol=1e-3
    )
