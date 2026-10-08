"""定位出发：固定入口、最新共享标注、新位置检索和独立预算。"""

import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from test_trip_candidates import CENTER, facility

from app import main, storage
from app.isochrone.geometry import haversine_m, offset_point
from app.markings import routes as marking_routes
from app.markings import store as marking_store
from app.trip import routes
from app.trip.budget import BudgetExhausted, TripBudget, Usage
from app.trip.candidates import candidates


@pytest.fixture
def client(trip_provider, tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "analyses.db")
    monkeypatch.setattr(marking_store, "DATA_DIR", tmp_path / "markings")
    monkeypatch.setattr(marking_routes, "_service", None)
    monkeypatch.setattr(main, "_config_warnings", lambda: [])
    with TestClient(main.app) as client:
        main.app.state.baidu = trip_provider
        yield client


def fixed_request(feature, places, origin=None):
    selected = [candidates(feature, place["category"], [], True)[0] for place in places]
    return {
        "feature": feature,
        "origin": origin or {"lat": 31.2501, "lng": 121.42},
        "stops": [p["category"] for p in places],
        "selected_stops": [n.as_dict() for n in selected],
        "places": places,
    }


def test_reanchor_keeps_exact_school_gate_and_order_without_alternatives(
    client, trip_feature, trip_provider
):
    market = facility("菜市场", 300, "生鲜采买")
    school = facility("小学", 700, "基础教育")
    school["entries"] = [
        {**facility("北门", 650), "name": "北门"},
        {**facility("南门", 750), "name": "南门"},
    ]
    trip_feature["properties"]["coverage"]["places"] = [
        market,
        school,
        facility("更近菜场", 40, "生鲜采买"),
    ]
    original = copy.deepcopy(trip_feature)
    req = fixed_request(trip_feature, [market, school])
    req["selected_stops"][0] = next(
        n.as_dict() for n in candidates(trip_feature, "生鲜采买", [], True) if n.name == "菜市场"
    )
    result = client.post("/api/trip/reanchor", json=req)
    assert result.status_code == 200, result.text
    data = result.json()
    assert data["routing_status"] == "clear"
    assert [i["name"] for i in data["legs"]] == ["菜市场", "小学"]
    assert data["legs"][1]["entry_id"] == req["selected_stops"][1]["entry_id"]
    assert data["legs"][1]["gate"] == "北门"
    assert data["alternatives"] == [] and data["quota"]["matrix_pairs"] == 2
    assert data["cache_policy"] == "memory"
    assert trip_feature == original
    assert not list(trip_provider._s.cache_dir.rglob("*.json"))


def test_fixed_destination_accepts_origin_outside_old_snapshot(client, trip_feature):
    store = facility("药店", 300)
    trip_feature["properties"]["coverage"]["places"] = [store]
    lat, lng = offset_point(*CENTER, 90, 4000)
    result = client.post(
        "/api/trip/reanchor", json=fixed_request(trip_feature, [store], {"lat": lat, "lng": lng})
    ).json()
    assert result["routing_status"] == "clear"
    assert result["routing_feature"]["properties"]["coverage"]["radius_m"] > 4000
    assert result["legs"][0]["name"] == "药店"


@pytest.mark.parametrize("kind", ["facility_missing", "closure"])
def test_latest_shared_evidence_blocks_fixed_plan_instead_of_switching_destination(
    client, trip_feature, monkeypatch, kind
):
    store = facility("药店", 500)
    trip_feature["properties"]["coverage"]["places"] = [store, facility("另一家药店", 600)]
    spec = (
        {**store, "reason": "closed"}
        if kind == "facility_missing"
        else {**facility("围挡", 250), "radius_m": 40, "kind": "construction"}
    )

    async def nearby(*args):
        return [{"id": 9, "type": kind, "spec": spec, "status": "verified", "mine": False}]

    monkeypatch.setattr(routes, "markings_for_analysis", nearby)
    result = client.post("/api/trip/reanchor", json=fixed_request(trip_feature, [store]))
    if kind == "facility_missing":
        assert result.status_code == 400
    else:
        assert result.status_code == 200
        assert result.json()["routing_status"] == "blocked"
        assert result.json()["legs"][0]["name"] == "药店"


def test_route_quota_failure_does_not_report_new_plan_as_clear(client, trip_feature, trip_provider):
    store = facility("药店", 300)
    trip_feature["properties"]["coverage"]["places"] = [store]
    trip_provider.test_answer = (
        lambda endpoint, params: {"status": 302, "message": "配额超限"}
        if "directionlite" in endpoint
        else None
    )
    result = client.post("/api/trip/reanchor", json=fixed_request(trip_feature, [store])).json()
    assert result["routing_status"] == "unverified"
    assert result["legs"][0]["route"] is None


def test_actual_detour_outside_snapshot_is_rechecked_without_extra_route_requests(
    client, trip_feature, trip_provider, monkeypatch
):
    store = facility("药店", 500)
    trip_feature["properties"]["coverage"]["places"] = [store]
    detour = offset_point(*CENTER, 90, 4000)
    queries = []

    async def evidence(lat, lng, radius, viewer):
        queries.append((lat, lng, radius))
        if haversine_m(lat, lng, *detour) <= radius:
            return [
                {
                    "id": 17,
                    "type": "closure",
                    "status": "verified",
                    "mine": False,
                    "spec": {
                        "lat": detour[0],
                        "lng": detour[1],
                        "radius_m": 20,
                        "kind": "construction",
                    },
                }
            ]
        return []

    monkeypatch.setattr(routes, "markings_for_analysis", evidence)

    def answer(endpoint, params):
        if "directionlite" in endpoint:
            a = tuple(map(float, params["origin"].split(",")))
            b = tuple(map(float, params["destination"].split(",")))
            path = ";".join(f"{lng},{lat}" for lat, lng in [a, detour, b])
            return {
                "status": 0,
                "result": {
                    "routes": [
                        {
                            "distance": 8100,
                            "duration": 7000,
                            "steps": [
                                {
                                    "path": path,
                                    "instruction": "绕行",
                                    "distance": 8100,
                                    "duration": 7000,
                                }
                            ],
                        }
                    ]
                },
            }

    trip_provider.test_answer = answer
    response = client.post("/api/trip/reanchor", json=fixed_request(trip_feature, [store]))
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(queries) == 2
    assert result["routing_status"] == "blocked"
    assert result["quota"]["matrix_pairs"] == result["quota"]["route_requests"] == 1


def test_poi_day_reset_keeps_hour_window(trip_provider, tmp_path, monkeypatch):
    from datetime import datetime

    import app.trip.budget as mod

    now = datetime(2026, 10, 8, 23, 59, tzinfo=mod.BEIJING).timestamp()
    monkeypatch.setattr(mod.time, "time", lambda: now)
    settings = replace(trip_provider._s, trip_day_pois=1, trip_hour_pois=1)
    budget = TripBudget(tmp_path / "window.sqlite3", settings)
    budget.take_poi(Usage())
    now += 120
    with pytest.raises(BudgetExhausted, match="小时"):
        budget.take_poi(Usage())
    now += 3601
    budget.take_poi(Usage())


def test_nearby_recollects_at_new_origin_and_never_writes_location_caches(
    client, trip_feature, trip_provider
):
    origin = {"lat": 40.01, "lng": 116.31}

    def answer(endpoint, params):
        if "/place/v2/search" in endpoint:
            lat, lng = map(float, params["location"].split(","))
            rows = []
            for i in range(6):
                a, b = offset_point(lat, lng, 0, 100 + i * 80)
                rows.append({"name": f"新位置药店{i}", "location": {"lat": a, "lng": b}})
            return {"status": 0, "total": 6, "results": rows}

    trip_provider.test_answer = answer
    req = {"feature": trip_feature, "origin": origin, "category": "医药"}
    original = copy.deepcopy(trip_feature)
    response = client.post("/api/trip/guide-nearby", json=req)
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["items"]) == 5 and result["items"][0]["name"] == "新位置药店0"
    assert result["routing_feature"]["properties"]["center"] == origin
    assert result["quota"]["poi_requests"] == 2 and result["cache_policy"] == "memory"
    repeated = client.post("/api/trip/guide-nearby", json=req).json()
    assert (
        repeated["quota"]["poi_requests"]
        == repeated["quota"]["matrix_pairs"]
        == repeated["quota"]["route_requests"]
        == 0
    )
    assert not list(trip_provider._s.cache_dir.rglob("*.json"))
    assert trip_feature == original


def test_nearby_poi_budget_stops_before_external_search(client, trip_feature, trip_provider):
    trip_provider._s = replace(trip_provider._s, trip_day_pois=0)
    result = client.post(
        "/api/trip/guide-nearby",
        json={"feature": trip_feature, "origin": {"lat": 31.26, "lng": 121.42}, "category": "医药"},
    )
    assert result.status_code == 429 and "预算" in result.json()["detail"]["message"]
    assert not trip_provider.test_hits


def test_poi_budget_is_atomic_across_instances(trip_provider, tmp_path):
    settings = replace(trip_provider._s, trip_day_pois=3, trip_hour_pois=100)
    path = tmp_path / "pois.sqlite3"

    def reserve(_):
        try:
            TripBudget(path, settings).take_poi(Usage())
            return 1
        except BudgetExhausted:
            return 0

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(reserve, range(12))) == 3
    with pytest.raises(BudgetExhausted):
        TripBudget(path, settings).take_poi(Usage())


@pytest.mark.parametrize("endpoint", ["reanchor", "guide-nearby"])
def test_simulation_cannot_become_current_location_guidance(client, trip_feature, endpoint):
    store = facility("药店", 300)
    trip_feature["properties"]["coverage"]["places"] = [store]
    trip_feature["properties"]["simulated"] = True
    req = (
        fixed_request(trip_feature, [store])
        if endpoint == "reanchor"
        else {"feature": trip_feature, "origin": {"lat": 31.25, "lng": 121.42}, "category": "医药"}
    )
    assert client.post(f"/api/trip/{endpoint}", json=req).status_code == 400
