"""共享标注状态、受阻重选和模拟隔离的专项验收；全部使用假百度传输。"""

import asyncio
import copy
from dataclasses import replace

from fastapi.testclient import TestClient
from test_trip_candidates import CENTER, facility

from app import main, storage
from app.isochrone.geometry import offset_point
from app.markings.apply import plan
from app.trip import routes
from app.trip.context import matches, overlay
from app.trip.service import TripService


def marking(ident, typ, spec, **kwargs):
    return {"id": ident, "type": typ, "spec": spec, "status": "verified", "mine": False, **kwargs}


def shared_closure(feature, bearing=0, distance=250, radius=30):
    lat, lng = offset_point(*CENTER, bearing, distance)
    feature["properties"]["markings"] = {
        "applied": [
            marking(
                9, "closure", {"kind": "construction", "lat": lat, "lng": lng, "radius_m": radius}
            )
        ]
    }


def nearest(provider, feature, limit=5):
    return asyncio.run(
        TripService(
            provider, copy.deepcopy(feature), {"lat": CENTER[0], "lng": CENTER[1], "kind": "center"}
        ).nearest("医药", limit)
    )


def test_shared_closure_replaces_blocked_store_and_rechecks_cached_paths(
    trip_feature, trip_provider
):
    trip_feature["properties"]["coverage"]["places"] = [
        facility("受阻药店", 500),
        facility("可行药店", 600, "医药", 90),
    ]
    initial = nearest(trip_provider, trip_feature, 1)
    assert initial["items"][0]["name"] == "受阻药店"
    shared_closure(trip_feature)
    changed = nearest(trip_provider, trip_feature, 1)
    assert changed["items"][0]["name"] == "可行药店"
    assert changed["items"][0]["closure_status"] == "clear"
    assert changed["blocked_count"] == 1
    assert changed["ranking_verified"]
    del trip_feature["properties"]["markings"]
    reopened = nearest(trip_provider, trip_feature, 1)
    assert reopened["items"][0]["name"] == "受阻药店"
    assert reopened["quota"]["route_requests"] == 0


def test_nearest_expands_past_first_five_and_does_not_recommend_unknown(
    trip_feature, trip_provider
):
    trip_feature["properties"]["coverage"]["places"] = [
        facility(f"受阻{j}", 400 + j * 30) for j in range(6)
    ] + [facility(f"通路{j}", 700 + j * 40, "医药", 90) for j in range(5)]
    shared_closure(trip_feature)
    result = nearest(trip_provider, trip_feature)
    assert len(result["items"]) == 5
    assert all(
        i["name"].startswith("通路") and i["closure_status"] == "clear" for i in result["items"]
    )
    assert result["blocked_count"] == 6
    trip_provider._s = replace(trip_provider._s, trip_request_routes=0)
    trip_provider._trip_memory.clear()
    # 新起点不能命中上一次路线缓存。
    origin = {"lat": CENTER[0] + 0.0001, "lng": CENTER[1], "kind": "point"}
    limited = asyncio.run(
        TripService(trip_provider, copy.deepcopy(trip_feature), origin).nearest("医药", 5)
    )
    assert limited["items"] == [] and limited["unverified_count"] > 0
    assert not limited["ranking_verified"]


def test_same_school_can_use_another_gate(trip_feature, trip_provider):
    school = facility("小学", 600, "基础教育")
    school["entries"] = [
        {
            **dict(zip(("lat", "lng"), offset_point(*CENTER, b, 500), strict=True)),
            "name": name,
            "basis": "gate",
        }
        for b, name in [(0, "北门"), (90, "东门")]
    ]
    trip_feature["properties"]["coverage"]["places"] = [school]
    shared_closure(trip_feature)
    result = asyncio.run(
        TripService(trip_provider, trip_feature, {"lat": CENTER[0], "lng": CENTER[1]}).nearest(
            "基础教育", 1
        )
    )
    assert result["items"][0]["gate"] == "东门"


def test_multistop_dp_reselects_when_the_first_connection_is_blocked(trip_feature, trip_provider):
    trip_feature["properties"]["coverage"]["places"] = [
        facility("近菜市场", 500, "生鲜采买"),
        facility("通路菜市场", 600, "生鲜采买", 90),
        facility("东药店", 900, "医药", 90),
    ]
    shared_closure(trip_feature)
    result = asyncio.run(
        TripService(trip_provider, trip_feature, {"lat": CENTER[0], "lng": CENTER[1]}).plan(
            ["生鲜采买", "医药"]
        )
    )
    assert [i["name"] for i in result["legs"]] == ["通路菜市场", "东药店"]
    assert result["routing_status"] == "clear"
    assert all(i["closure_status"] == "clear" for i in result["legs"])
    assert result["quota"]["route_requests"] <= trip_provider._s.trip_request_routes
    selected = [{"place_id": i["place_id"], "entry_id": i["entry_id"]} for i in result["legs"]]
    manual = asyncio.run(
        TripService(trip_provider, trip_feature, {"lat": CENTER[0], "lng": CENTER[1]}).plan(
            ["生鲜采买", "医药"], selected
        )
    )
    assert manual["optimality"] == "manual"


def test_overlay_conflicts_and_retraction_restore_original_places(trip_feature):
    existing = facility("原药店", 100)
    spec = {k: existing[k] for k in ("name", "category", "lat", "lng")}
    props = trip_feature["properties"]
    props["coverage"]["places"] = []
    props["markings"] = {"baseline": {"places": [existing]}}
    removed = marking(1, "facility_missing", {**spec, "reason": "closed"}, mine=True)
    extra = marking(2, "facility_extra", spec, status="pending")
    overlay(trip_feature, plan([extra, removed], "all"))
    assert props["coverage"]["places"] == []
    overlay(trip_feature, plan([], "auto"))
    assert props["coverage"]["places"] == [existing]
    neighbour = {**spec, "name": "邻居药店"}
    assert not matches(neighbour, spec)


def test_newer_own_closure_record_overrides_older_own_facility_confirmation(trip_feature):
    existing = facility("药店", 100)
    spec = {k: existing[k] for k in ("name", "category", "lat", "lng")}
    props = trip_feature["properties"]
    props["coverage"]["places"] = [existing]
    old = marking(1, "facility_extra", spec, mine=True, updated_at="2026-10-01T00:00:00+08:00")
    new = marking(
        2,
        "facility_missing",
        {**spec, "reason": "closed"},
        mine=True,
        status="pending",
        updated_at="2026-10-07T00:00:00+08:00",
    )
    older = [
        marking(
            i,
            "facility_extra",
            {**spec, "name": f"其他药店{i}"},
            mine=True,
            updated_at="2026-10-01T00:00:00+08:00",
        )
        for i in range(3, 12)
    ]
    overlay(trip_feature, plan([old, new, *older], "auto"))
    assert all(p["name"] != "药店" for p in props["coverage"]["places"])
    assert 2 in [m["id"] for m in props["markings"]["applied"]]


def test_http_reads_current_shared_records_and_ignores_simulated_closure(
    trip_feature, trip_provider, monkeypatch, tmp_path
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "history.sqlite3")
    monkeypatch.setattr(main, "_config_warnings", lambda: [])
    shop = facility("新药店", 500)
    spec = {k: shop[k] for k in ("name", "category", "lat", "lng")}
    active = [marking(1, "facility_extra", spec)]

    async def nearby(*args):
        return copy.deepcopy(active)

    monkeypatch.setattr(routes, "markings_for_analysis", nearby)
    lat, lng = offset_point(*CENTER, 0, 250)
    trip_feature["properties"]["closures"] = [{"lat": lat, "lng": lng, "radius_m": 30}]
    request = {
        "feature": trip_feature,
        "origin": {"lat": CENTER[0], "lng": CENTER[1]},
        "category": "医药",
    }
    with TestClient(main.app) as client:
        main.app.state.baidu = trip_provider
        response = client.post("/api/trip/nearest", json=request)
        assert response.status_code == 200
        assert response.json()["items"][0]["name"] == "新药店"
        active.append(marking(2, "facility_missing", {**spec, "reason": "closed"}, mine=True))
        assert client.post("/api/trip/nearest", json=request).json()["items"] == []
        active.pop()  # 撤回失效记录，仍有效的补录点重新可选。
        assert client.post("/api/trip/nearest", json=request).json()["items"][0]["name"] == "新药店"
        active.clear()  # 已撤回/过期记录不会由服务端有效记录查询返回。
        assert client.post("/api/trip/nearest", json=request).json()["items"] == []


def test_closure_preview_never_saves_formal_history(trip_feature, trip_provider, monkeypatch):
    monkeypatch.setattr(main, "_config_warnings", lambda: [])

    async def steps(*args):
        yield "report", {"feature": copy.deepcopy(trip_feature)}

    monkeypatch.setattr(main, "_analysis_steps", steps)

    def forbidden(*args, **kwargs):
        raise AssertionError("preview must not save history")

    monkeypatch.setattr(storage, "save_analysis", forbidden)
    original = copy.deepcopy(trip_feature)
    with TestClient(main.app) as client:
        main.app.state.baidu = trip_provider
        response = client.post(
            "/api/simulate/closures", json={"feature": trip_feature, "closures": []}
        )
    assert response.status_code == 200
    assert response.json()["properties"]["planning_preview"] is True
    assert trip_feature == original


def test_preview_runs_real_analysis_pipeline_with_mock_baidu(
    trip_feature, trip_provider, monkeypatch
):
    monkeypatch.setattr(main, "_config_warnings", lambda: [])
    trip_feature["properties"]["minutes"] = 1
    trip_feature["properties"]["rays"] = [{}] * 8
    trip_feature["properties"]["blindspots"]["grid_spacing_m"] = 200
    lat, lng = offset_point(*CENTER, 0, 20)
    with TestClient(main.app) as client:
        main.app.state.baidu = trip_provider
        response = client.post(
            "/api/simulate/closures",
            json={"feature": trip_feature, "closures": [{"lat": lat, "lng": lng, "radius_m": 10}]},
        )
    assert response.status_code == 200
    props = response.json()["properties"]
    assert props["planning_preview"] and props["report"]
    assert props["quality"]["closure_truncated"] > 0
    assert props["planning_baseline"]["report"]
    assert props["area_km2"] <= props["planning_baseline"]["area_km2"]


def test_closure_preview_compares_current_baseline_instead_of_old_snapshot(
    trip_feature, trip_provider, monkeypatch
):
    monkeypatch.setattr(main, "_config_warnings", lambda: [])
    calls = []
    trip_feature["properties"]["report"] = {"total": 90}

    async def steps(client, request, *args):
        calls.append(len(request.closures))
        feature = copy.deepcopy(trip_feature)
        feature["properties"]["report"] = {"total": 55 if request.closures else 60}
        feature["properties"]["area_km2"] = 1 if request.closures else 2
        yield "report", {"feature": feature}

    monkeypatch.setattr(main, "_analysis_steps", steps)
    with TestClient(main.app) as client:
        main.app.state.baidu = trip_provider
        response = client.post(
            "/api/simulate/closures",
            json={
                "feature": trip_feature,
                "closures": [{"lat": CENTER[0], "lng": CENTER[1], "radius_m": 10}],
            },
        )
    props = response.json()["properties"]
    assert calls == [0, 1]
    assert props["planning_baseline"] == {"report": {"total": 60}, "area_km2": 2}
    assert props["report"]["total"] == 55
    assert trip_feature["properties"]["report"]["total"] == 90


def test_formal_trip_refuses_preview_input(trip_feature, trip_provider, monkeypatch):
    trip_feature["properties"]["planning_preview"] = True
    with TestClient(main.app) as client:
        main.app.state.baidu = trip_provider
        response = client.post(
            "/api/trip/nearest",
            json={
                "feature": trip_feature,
                "origin": {"lat": CENTER[0], "lng": CENTER[1]},
                "category": "医药",
            },
        )
    assert response.status_code == 400 and response.json()["detail"]["code"] == "planning_preview"


def test_origin_inside_shared_closure_is_never_recommended(trip_feature, trip_provider):
    trip_feature["properties"]["coverage"]["places"] = [facility("药店", 500)]
    shared_closure(trip_feature, distance=0)
    result = nearest(trip_provider, trip_feature)
    assert result["items"] == [] and result["blocked_count"] == 1
    assert result["quota"]["route_requests"] == 0


def test_trip_returns_facility_center_for_marking_instead_of_navigation_entry(
    trip_feature, trip_provider
):
    place = facility("有导航入口的药店", 500)
    lat, lng = offset_point(place["lat"], place["lng"], 90, 100)
    place["entries"] = [{"lat": lat, "lng": lng, "name": "入口"}]
    trip_feature["properties"]["coverage"]["places"] = [place]
    item = nearest(trip_provider, trip_feature, 1)["items"][0]
    assert item["lat"] == round(lat, 6)
    assert item["place"]["lat"] == place["lat"]
    assert item["place"]["lng"] == place["lng"]
    assert item["place"]["id"] == item["place_id"]


def test_hypothetical_facility_updates_coverage_and_does_not_mutate_source():
    from test_simulate import FakeClient, _feature

    from app.report.simulate import simulate_facility

    feature = _feature()
    lat, lng = CENTER
    feature["geometry"]["coordinates"] = [
        [
            [lng - 0.02, lat - 0.02],
            [lng + 0.02, lat - 0.02],
            [lng + 0.02, lat + 0.02],
            [lng - 0.02, lat + 0.02],
            [lng - 0.02, lat - 0.02],
        ]
    ]
    original = copy.deepcopy(feature)
    result = asyncio.run(simulate_facility(FakeClient([100, 1200]), feature, "医药", *CENTER))
    # 本品类从0家变为1家；改善一个格子的均衡分之外，覆盖分也应改善。
    assert result["after"]["score"] - result["before"]["score"] > 20
    assert result["covered_count"] == 1
    assert feature == original


def test_simulation_checks_known_closures_and_rejects_unknown_routes():
    from test_simulate import FakeClient, _feature

    from app.report.simulate import simulate_facility

    class Client(FakeClient):
        unknown = False

        async def walking_route(self, origin, destination):
            return None if self.unknown else {"steps": [{"path": [origin, destination]}]}

    feature = _feature()
    shared_closure(feature)
    provider = Client([100, 100])
    checked = asyncio.run(simulate_facility(provider, feature, "医药", *CENTER))
    assert checked["covered_count"] == 1 and checked["route_checks"] == 2
    provider.unknown = True
    unknown = asyncio.run(simulate_facility(provider, feature, "医药", *CENTER))
    assert unknown["covered_count"] == 0
    shared_closure(feature, distance=0)
    inside = asyncio.run(simulate_facility(provider, feature, "医药", *CENTER))
    assert inside["covered_count"] == 0 and inside["after"] == inside["before"]
