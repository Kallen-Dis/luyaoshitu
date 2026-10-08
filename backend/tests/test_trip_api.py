import asyncio
import copy
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from test_trip_candidates import CENTER, facility

from app import main, storage
from app.trip.budget import BudgetExhausted, TripBudget, Usage
from app.trip.service import TripService


def nearest(provider, feature, origin=None):
    return asyncio.run(
        TripService(
            provider,
            copy.deepcopy(feature),
            origin or {"lat": CENTER[0], "lng": CENTER[1], "kind": "center"},
        ).nearest("医药", 5)
    )


def test_cache_is_exact_and_resident_queries_never_write_coordinates_to_disk(
    trip_feature, trip_provider
):
    trip_feature["properties"]["coverage"]["places"] = [facility("药店", 400)]
    origin = {"lat": 31.25001, "lng": 121.42, "kind": "map"}
    first = nearest(trip_provider, trip_feature, origin)
    second = nearest(trip_provider, trip_feature, origin)
    assert first["quota"]["matrix_pairs"] == 1
    assert second["quota"]["matrix_pairs"] == 0 and second["quota"]["route_requests"] == 0
    assert not list(trip_provider._s.cache_dir.rglob("*.json"))
    shifted = nearest(trip_provider, trip_feature, {**origin, "lat": 31.25002})
    assert shifted["quota"]["matrix_pairs"] == 1
    assert shifted["items"][0]["route"]["path"][0] == [121.42, 31.25002]


def test_matrix_distance_is_ranked_and_displayed_while_route_distance_is_diagnostic(
    trip_feature, trip_provider
):
    trip_feature["properties"]["coverage"]["places"] = [facility("药店", 400)]
    result = nearest(trip_provider, trip_feature)
    item = result["items"][0]
    assert item["walk_m"] != round(item["route"]["distance_m"], 1)
    assert item["route"]["path"][0] == [121.42, 31.25]
    assert item["route"]["steps"][0]["path"][0] == [121.42, 31.25]
    assert item["route"]["steps"][0]["path"][-1] == item["route"]["path"][-1]
    assert item["route"]["crossings"]["过街"] == 1
    assert nearest(trip_provider, trip_feature)["quota"]["matrix_pairs"] == 0


def test_route_quota_keeps_matrix_result_and_failed_route_does_not_fake_clear(
    trip_feature, trip_provider
):
    trip_feature["properties"]["coverage"]["places"] = [facility("药店", 400)]
    trip_provider.test_answer = (
        lambda endpoint, params: {"status": 302, "message": "天配额超限"}
        if "directionlite" in endpoint
        else None
    )
    result = nearest(trip_provider, trip_feature)
    assert result["basis"] == "network"
    assert result["items"][0]["route"] is None
    assert result["items"][0]["closure_status"] == "unverified"
    assert "配额" in result["items"][0]["note"]


def test_atomic_day_budget_is_shared_and_retries_are_charged(trip_feature, trip_provider):
    settings = replace(
        trip_provider._s, trip_day_pairs=3, trip_hour_pairs=10, trip_request_pairs=10
    )
    path = settings.markings_dir / "test.sqlite3"
    budget = TripBudget(path, settings)
    budget.take(Usage(), pairs=2)
    with pytest.raises(BudgetExhausted):
        TripBudget(path, settings).take(Usage(), pairs=2)
    assert budget.remaining(Usage())["day_pairs"] == 1
    trip_provider._s = replace(trip_provider._s, max_retries=1, retry_backoff=0)
    trip_feature["properties"]["coverage"]["places"] = [facility("药店", 400)]
    counter = []

    def response(endpoint, params):
        if "routematrix" in endpoint and not counter:
            counter.append(1)
            return {"status": 401, "message": "限流"}

    trip_provider.test_answer = response
    result = nearest(trip_provider, trip_feature)
    assert result["quota"]["matrix_pairs"] == 2 and result["quota"]["matrix_requests"] == 2


def test_concurrent_budget_reservations_never_exceed_day_cap(trip_provider):
    from concurrent.futures import ThreadPoolExecutor

    settings = replace(trip_provider._s, trip_day_pairs=10, trip_hour_pairs=100)
    path = settings.markings_dir / "concurrent.sqlite3"

    def reserve(_):
        try:
            TripBudget(path, settings).take(Usage(), pairs=3)
            return 3
        except BudgetExhausted:
            return 0

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(reserve, range(8))) == 9
    assert TripBudget(path, settings).remaining(Usage())["day_pairs"] == 1


def test_day_reset_preserves_the_sliding_hour_window(trip_provider, monkeypatch):
    from datetime import datetime

    import app.trip.budget as mod

    now = datetime(2026, 10, 7, 23, 59, tzinfo=mod.BEIJING).timestamp()
    monkeypatch.setattr(mod.time, "time", lambda: now)
    settings = replace(trip_provider._s, trip_day_pairs=10, trip_hour_pairs=10)
    budget = TripBudget(settings.markings_dir / "clock.sqlite3", settings)
    budget.take(Usage(), pairs=8)
    now += 120
    assert budget.remaining(Usage())["day_pairs"] == 10
    assert budget.remaining(Usage())["hour_pairs"] == 2
    with pytest.raises(BudgetExhausted):
        budget.take(Usage(), pairs=3)
    now += 3601
    budget.take(Usage(), pairs=3)
    assert budget.remaining(Usage())["day_pairs"] == 7


def test_failed_matrix_is_unknown_and_zero_budget_uses_estimate(trip_feature, trip_provider):
    trip_feature["properties"]["coverage"]["places"] = [facility("药店", 300)]
    trip_provider.test_answer = (
        lambda endpoint, params: {"status": 0, "result": [{}]}
        if "routematrix" in endpoint
        else None
    )
    result = nearest(trip_provider, trip_feature)
    assert result["basis"] == "estimate_error" and not result["ranking_verified"]
    assert any("未知" in w for w in result["warnings"])
    assert not any("已测候选中的最近" in w for w in result["warnings"])
    trip_provider.test_answer = None
    trip_provider._s = replace(trip_provider._s, trip_day_pairs=0)
    result = nearest(trip_provider, trip_feature, {"lat": 31.25003, "lng": 121.42, "kind": "map"})
    assert result["basis"] == "estimate_quota" and result["quota"]["matrix_pairs"] == 0


def test_trip_api_success_limits_and_category_failures(
    trip_feature, trip_provider, tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "api.sqlite3")
    monkeypatch.setattr(main, "_config_warnings", lambda: [])
    trip_feature["properties"]["coverage"]["places"] = [facility("药店", 300)]
    with TestClient(main.app) as client:
        main.app.state.baidu = trip_provider
        request = {
            "feature": trip_feature,
            "origin": {"lat": 31.25, "lng": 121.42, "kind": "center"},
            "category": "医药",
        }
        first = client.post("/api/trip/nearest", json=request)
        assert first.status_code == 200 and first.json()["items"]
        second = client.post("/api/trip/nearest", json=request).json()
        assert second["quota"]["matrix_pairs"] == second["quota"]["route_requests"] == 0
        one = client.post(
            "/api/trip/plan",
            json={"feature": trip_feature, "origin": request["origin"], "stops": ["医药"]},
        ).json()
        assert one["legs"][0]["entry_id"] == first.json()["items"][0]["entry_id"]
        duplicate = client.post(
            "/api/trip/plan",
            json={"feature": trip_feature, "origin": request["origin"], "stops": ["医药", "医药"]},
        )
        assert duplicate.status_code == 400
        trip_feature["properties"]["coverage"]["failed_categories"] = ["医药"]
        assert (
            client.post("/api/trip/nearest", json=request).json()["detail"]["code"]
            == "category_failed"
        )
        del trip_feature["properties"]["coverage"]["places"]
        assert (
            client.post("/api/trip/nearest", json=request).json()["detail"]["code"] == "no_places"
        )


def test_api_rate_limit_counts_cached_calls_across_endpoints(
    trip_feature, trip_provider, tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "api.sqlite3")
    monkeypatch.setattr(main, "_config_warnings", lambda: [])
    trip_provider._s = replace(trip_provider._s, trip_hour_requests=2)
    trip_feature["properties"]["coverage"]["places"] = [facility("药店", 300)]
    origin = {"lat": 31.25, "lng": 121.42, "kind": "center"}
    with TestClient(main.app) as client:
        main.app.state.baidu = trip_provider
        nearest_req = {"feature": trip_feature, "origin": origin, "category": "医药"}
        assert client.post("/api/trip/nearest", json=nearest_req).status_code == 200
        assert client.post("/api/trip/nearest", json=nearest_req).status_code == 200
        response = client.post(
            "/api/trip/plan", json={"feature": trip_feature, "origin": origin, "stops": ["医药"]}
        )
        assert response.status_code == 429
        assert response.json()["detail"]["code"] == "trip_rate_limited"
    # 新实例同样读到这份额度，服务重启不会清空。
    assert not TripBudget(
        trip_provider._s.markings_dir / "trip-budget.sqlite3", trip_provider._s
    ).take_request()


@pytest.mark.parametrize("budget", [5, 10])
def test_request_budget_preserves_measured_candidates(trip_feature, trip_provider, budget):
    trip_provider._s = replace(trip_provider._s, trip_request_pairs=budget)
    trip_feature["properties"]["coverage"]["places"] = [
        facility(f"药店{i}", 100 + i * 10) for i in range(20)
    ]

    def answer(endpoint, params):
        if "routematrix" in endpoint:
            return {
                "status": 0,
                "result": [
                    {"distance": {"value": 2000}, "duration": {"value": 1700}}
                    for p in params["destinations"].split("|")
                ],
            }

    trip_provider.test_answer = answer
    result = nearest(trip_provider, trip_feature)
    assert result["basis"] == "network" and result["items"]
    assert not result["ranking_verified"]
    assert result["quota"]["matrix_pairs"] == budget


def test_actual_closure_is_skipped_by_default_and_reported_for_pinned_target(
    trip_feature, trip_provider
):
    from app.isochrone.geometry import offset_point

    trip_feature["properties"]["coverage"]["places"] = [facility("药店", 500)]
    lat, lng = offset_point(*CENTER, 0, 250)
    trip_feature["properties"]["closures"] = [{"lat": lat, "lng": lng, "radius_m": 30}]
    result = nearest(trip_provider, trip_feature)
    assert result["items"] == []
    assert result["blocked_count"] == 1
    from app.trip.candidates import place_id

    pinned = asyncio.run(
        TripService(
            trip_provider, trip_feature, {"lat": CENTER[0], "lng": CENTER[1], "kind": "center"}
        ).nearest("医药", 1, place_id(trip_feature["properties"]["coverage"]["places"][0]))
    )
    assert pinned["items"][0]["closure_status"] == "blocked"
    assert pinned["items"][0]["route"]["closure_entry"]
    assert not pinned["ranking_verified"]


@pytest.mark.parametrize(
    "payload,code",
    [
        ({"category": "未知"}, "invalid"),
        ({"limit": 6}, "invalid"),
        ({"origin": {"lat": float("nan"), "lng": 121.42}}, "invalid"),
        ({"origin": {"lat": 31.35, "lng": 121.42}}, "origin_out_of_range"),
    ],
)
def test_api_validation(trip_feature, trip_provider, tmp_path, monkeypatch, payload, code):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "analyses.db")
    monkeypatch.setattr(main, "_require_server_ak", lambda: None)
    # install 的依赖函数在安装时捕获；设置配置检查而不是触发任何真实服务。
    monkeypatch.setattr(main, "_config_warnings", lambda: [])
    with TestClient(main.app) as client:
        main.app.state.baidu = trip_provider
        req = {
            "feature": trip_feature,
            "origin": {"lat": 31.25, "lng": 121.42},
            "category": "医药",
            **payload,
        }
        if code == "invalid" and "origin" in payload:
            req["origin"] = {"lat": "nan", "lng": 121.42}
        response = client.post("/api/trip/nearest", json=req)
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == code
