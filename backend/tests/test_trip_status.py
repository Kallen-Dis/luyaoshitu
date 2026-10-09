"""实际服务响应约定与局部补取：全程状态不能只由前端假数据保证。"""

import asyncio
import copy

import pytest
from fastapi.testclient import TestClient
from test_trip_candidates import CENTER, facility

from app import main, storage
from app.trip.service import TripService
from app.trip.status import plan_status

ORIGIN = {"lat": CENTER[0], "lng": CENTER[1], "kind": "center"}


def two_stops(feature):
    school = facility("小学", 600, "基础教育", 35)
    school["entries"] = [{**facility("西门", 550, "基础教育", 35), "name": "西门"}]
    feature["properties"]["coverage"]["places"] = [facility("药店", 200), school]
    return ["医药", "基础教育"]


def plan(provider, feature, stops, selected=None, retry=False):
    return asyncio.run(
        TripService(provider, copy.deepcopy(feature), ORIGIN).plan(
            stops, selected, retry_routes=retry
        )
    )


@pytest.mark.parametrize("count", [1, 2])
def test_normal_api_plan_returns_clear_status_with_continuous_usable_legs(
    trip_feature, trip_provider, tmp_path, monkeypatch, count
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "api.sqlite3")
    monkeypatch.setattr(main, "_config_warnings", lambda: [])
    stops = two_stops(trip_feature)[:count]
    with TestClient(main.app) as client:
        main.app.state.baidu = trip_provider
        response = client.post(
            "/api/trip/plan",
            json={"feature": trip_feature, "origin": ORIGIN, "stops": stops},
        )
    assert response.status_code == 200
    result = response.json()
    assert result["routing_status"] == "clear"
    assert (
        plan_status(result["origin"], result["stops"], result["legs"], result["basis"]) == "clear"
    )
    assert all(leg["route"]["path"] and leg["route"]["steps"] for leg in result["legs"])
    if count == 2:
        assert result["legs"][1]["gate"] == "西门"
        assert result["legs"][1]["from"]["lat"] == result["legs"][0]["lat"]


@pytest.mark.parametrize("case", ["empty", "estimated", "missing-route"])
def test_degraded_plan_branches_explicitly_remain_unverified(trip_feature, trip_provider, case):
    stops = two_stops(trip_feature)
    if case == "empty":
        trip_feature["properties"]["coverage"]["places"].pop()
    elif case == "estimated":
        trip_feature["properties"]["simulated"] = True
    else:
        trip_provider.test_answer = (
            lambda endpoint, params: {"status": 0, "result": {"routes": []}}
            if "directionlite" in endpoint
            else None
        )
    result = plan(trip_provider, trip_feature, stops)
    assert result["routing_status"] == "unverified"
    assert not any(leg.get("closure_status") == "blocked" for leg in result["legs"])


@pytest.mark.parametrize("malformed_cache", [False, True])
def test_retry_pins_original_gates_and_only_queries_missing_route(
    trip_feature, trip_provider, malformed_cache
):
    stops = two_stops(trip_feature)

    def missing_second(endpoint, params):
        if "directionlite" in endpoint and params["origin"] != "31.250000,121.420000":
            return {
                "status": 0,
                "result": {
                    "routes": [{"distance": 500, "duration": 400, "steps": []}]
                    if malformed_cache
                    else []
                },
            }

    trip_provider.test_answer = missing_second
    first = plan(trip_provider, trip_feature, stops)
    assert len(first["legs"]) == 2 and first["routing_status"] == "unverified"
    assert first["legs"][0]["closure_status"] == "clear"
    assert first["legs"][1]["route"] is None
    selected = [{"place_id": leg["place_id"], "entry_id": leg["entry_id"]} for leg in first["legs"]]
    trip_provider.test_hits.clear()
    trip_provider.test_answer = None
    retried = plan(trip_provider, trip_feature, stops, selected, retry=True)
    assert retried["routing_status"] == "clear"
    assert [leg["entry_id"] for leg in retried["legs"]] == [s["entry_id"] for s in selected]
    assert retried["legs"][1]["gate"] == "西门"
    hits = trip_provider.test_hits
    assert len(hits) == 1 and hits[0][0] == "/directionlite/v1/walking"
    assert hits[0][1]["origin"] != "31.250000,121.420000"
    assert retried["quota"]["matrix_pairs"] == 0
    assert retried["quota"]["route_requests"] == 1
    assert retried["alternatives"] == []


def test_retry_api_rejects_implicit_reselection(trip_feature, trip_provider, tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "api.sqlite3")
    monkeypatch.setattr(main, "_config_warnings", lambda: [])
    stops = two_stops(trip_feature)
    with TestClient(main.app) as client:
        main.app.state.baidu = trip_provider
        response = client.post(
            "/api/trip/plan",
            json={"feature": trip_feature, "origin": ORIGIN, "stops": stops, "retry_routes": True},
        )
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "invalid"
    assert not trip_provider.test_hits


def test_summary_does_not_call_incomplete_or_disconnected_legs_clear():
    first = {
        "category": "医药",
        "from": ORIGIN,
        "lat": 31.251,
        "lng": 121.42,
        "closure_status": "clear",
        "route": {"path": [[121.42, 31.25], [121.42, 31.251]], "steps": [{}]},
    }
    second = {
        **first,
        "category": "基础教育",
        "from": {"lat": 31.251, "lng": 121.42},
        "lat": 31.253,
        "lng": 121.42,
    }
    assert plan_status(ORIGIN, ["医药", "基础教育"], [first, second], "network") == "clear"
    disconnected = {**second, "from": {"lat": 31.252, "lng": 121.42}}
    assert (
        plan_status(ORIGIN, ["医药", "基础教育"], [first, disconnected], "network") == "unverified"
    )
    assert plan_status(ORIGIN, ["医药", "基础教育"], [first], "network") == "unverified"
    assert (
        plan_status(ORIGIN, ["医药"], [{**first, "closure_status": "blocked"}], "network")
        == "blocked"
    )
    assert plan_status(ORIGIN, ["医药"], [{**first, "route": {}}], "network") == "unverified"
