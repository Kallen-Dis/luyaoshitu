"""行程设置中的具体设施选择：浏览不算路，指定站点和校门不能被自动替换。"""

import asyncio
import copy
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from test_trip_candidates import CENTER, facility

from app import main, storage
from app.poi.catalog import CATEGORIES
from app.poi.named import remember
from app.trip.candidates import TripError, candidates
from app.trip.service import TripService

ORIGIN = {"lat": CENTER[0], "lng": CENTER[1], "kind": "center"}


def _shop_record(name="新发现生鲜超市", radius=250):
    place = facility(name, radius, "生鲜采买", 45)
    return {"name": name, "location": {"lat": place["lat"], "lng": place["lng"]}}


def test_options_include_previous_named_and_keyword_recall_without_network(
    trip_feature, trip_provider, tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "recalled-options.sqlite3")
    monkeypatch.setattr(main, "_config_warnings", lambda: [])
    feature = _choices(trip_feature)
    feature["properties"]["coverage"]["search_metadata"] = {
        "生鲜采买": [{"keyword": "生鲜", "complete": True}]
    }
    original = copy.deepcopy(feature)
    remember(trip_provider, CENTER, [_shop_record()])
    radius = feature["properties"]["coverage"]["radius_m"]
    path = trip_provider._cache.key_for_point("poi", *CENTER, "生鲜", radius, 0)
    trip_provider._cache.write(path, {"results": [_shop_record("补检索生鲜店", 350)], "total": 1})
    trip_provider._s = replace(trip_provider._s, trip_hour_requests=0, server_ak="")
    with TestClient(main.app) as client:
        main.app.state.baidu = trip_provider
        result = client.post(
            "/api/trip/options",
            json={"feature": feature, "origin": ORIGIN, "categories": ["生鲜采买"]},
        )
        assert result.status_code == 200
        assert {n["name"] for n in result.json()["groups"][0]["items"]} == {
            "较近菜市场",
            "指定生鲜超市",
            "新发现生鲜超市",
            "补检索生鲜店",
        }
        assert not trip_provider.test_hits
        chosen = next(n for n in result.json()["groups"][0]["items"] if n["name"] == "补检索生鲜店")
        trip_provider._s = replace(trip_provider._s, trip_hour_requests=100, server_ak="test-ak")
        planned = client.post(
            "/api/trip/plan",
            json={
                "feature": feature,
                "origin": ORIGIN,
                "stops": ["生鲜采买"],
                "fixed_stops": [
                    {"index": 0, "place_id": chosen["place_id"], "entry_id": chosen["entry_id"]}
                ],
            },
        )
        assert planned.status_code == 200
        assert planned.json()["legs"][0]["entry_id"] == chosen["entry_id"]
    assert feature == original


def test_named_lookup_is_explicit_budgeted_and_can_be_selected_in_the_next_plan(
    trip_feature, trip_provider, tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "lookup-choice.sqlite3")
    monkeypatch.setattr(main, "_config_warnings", lambda: [])
    feature = _choices(trip_feature)
    record = _shop_record("新发现生鲜超市（曹杨店）")

    def answer(endpoint, params):
        if endpoint == "/reverse_geocoding/v3/":
            return {"status": 0, "result": {"addressComponent": {"city": "上海市"}}}
        if endpoint == "/place/v2/suggestion":
            return {"status": 0, "result": [{"uid": "new-shop", "name": record["name"]}]}
        if endpoint == "/place/v2/detail":
            return {"status": 0, "result": record}

    trip_provider.test_answer = answer
    with TestClient(main.app) as client:
        main.app.state.baidu = trip_provider
        result = client.post(
            "/api/trip/options",
            json={
                "feature": feature,
                "origin": ORIGIN,
                "categories": ["生鲜采买"],
                "poi_query": "曹杨店",
            },
        )
        assert result.status_code == 200
        value = result.json()
        assert value["quota"]["poi_requests"] == 3
        assert value["quota"]["matrix_pairs"] == value["quota"]["route_requests"] == 0
        assert len(trip_provider.test_hits) == 3
        chosen = next(
            node for node in value["groups"][0]["items"] if record["name"] in node["aliases"]
        )
        assert chosen["name"] == "新发现生鲜超市"
        response = client.post(
            "/api/trip/plan",
            json={
                "feature": feature,
                "origin": ORIGIN,
                "stops": ["生鲜采买"],
                "fixed_stops": [
                    {"index": 0, "place_id": chosen["place_id"], "entry_id": chosen["entry_id"]}
                ],
            },
        )
        assert response.status_code == 200
        assert response.json()["legs"][0]["entry_id"] == chosen["entry_id"]


@pytest.mark.parametrize(
    "restriction", ["hour_requests", "poi_budget", "wrong_category", "simulation"]
)
def test_named_lookup_limits_and_invalid_queries_do_not_issue_network_requests(
    trip_feature, trip_provider, tmp_path, monkeypatch, restriction
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "lookup-limits.sqlite3")
    monkeypatch.setattr(main, "_config_warnings", lambda: [])
    feature = _choices(trip_feature)
    categories = ["生鲜采买"]
    if restriction == "hour_requests":
        trip_provider._s = replace(trip_provider._s, trip_hour_requests=0)
    elif restriction == "poi_budget":
        trip_provider._s = replace(trip_provider._s, trip_request_pois=0)
    elif restriction == "wrong_category":
        categories = ["医药"]
    else:
        feature["properties"]["simulated"] = True
    with TestClient(main.app) as client:
        main.app.state.baidu = trip_provider
        response = client.post(
            "/api/trip/options",
            json={
                "feature": feature,
                "origin": ORIGIN,
                "categories": categories,
                "poi_query": "新发现",
            },
        )
        assert response.status_code == (
            429 if restriction in ("hour_requests", "poi_budget") else 400
        )
    assert not trip_provider.test_hits


def _choices(feature):
    school = facility("学校", 600, "基础教育", 70)
    school["entries"] = [
        {**facility("东门", 550, bearing=60), "name": "东门"},
        {**facility("西门", 650, bearing=80), "name": "西门"},
    ]
    feature["properties"]["coverage"]["places"] = [
        facility("较近菜市场", 200, "生鲜采买"),
        facility("指定生鲜超市", 700, "生鲜采买", 25),
        facility("药店甲", 350, bearing=40),
        facility("药店乙", 450, bearing=50),
        school,
    ]
    return feature


def _pin(feature, index, category, name, gate=None):
    node = next(n for n in candidates(feature, category, []) if n.name == name and n.gate == gate)
    return {"index": index, "place_id": node.place_id, "entry_id": node.id}


def test_partial_fixed_choice_keeps_named_store_and_automatically_selects_other_stop(
    trip_feature, trip_provider
):
    feature = _choices(trip_feature)
    chosen = _pin(feature, 0, "生鲜采买", "指定生鲜超市")
    result = asyncio.run(
        TripService(trip_provider, feature, ORIGIN).plan(["生鲜采买", "医药"], fixed_stops=[chosen])
    )
    assert result["legs"][0]["name"] == "指定生鲜超市"
    assert result["legs"][0]["entry_id"] == chosen["entry_id"]
    assert result["legs"][1]["name"] in ("药店甲", "药店乙")
    assert result["optimality"] == "constrained"
    assert result["fixed_stops"] == [chosen]


@pytest.mark.parametrize("with_closure", [False, True])
def test_explicit_store_and_school_gate_are_preserved_with_or_without_closures(
    trip_feature, trip_provider, with_closure
):
    feature = _choices(trip_feature)
    if with_closure:
        distant = facility("远处围挡", 2000, bearing=180)
        feature["properties"]["closures"] = [
            {"lat": distant["lat"], "lng": distant["lng"], "radius_m": 30}
        ]
    chosen = [
        _pin(feature, 0, "生鲜采买", "指定生鲜超市"),
        _pin(feature, 1, "基础教育", "学校", "西门"),
    ]
    result = asyncio.run(
        TripService(trip_provider, feature, ORIGIN).plan(
            ["生鲜采买", "基础教育"], fixed_stops=chosen
        )
    )
    assert [leg["entry_id"] for leg in result["legs"]] == [p["entry_id"] for p in chosen]
    assert result["legs"][1]["gate"] == "西门"
    assert result["optimality"] == "manual"


def test_one_stop_with_explicit_facility_does_not_fall_back_to_nearest(trip_feature, trip_provider):
    feature = _choices(trip_feature)
    chosen = _pin(feature, 0, "生鲜采买", "指定生鲜超市")
    result = asyncio.run(
        TripService(trip_provider, feature, ORIGIN).plan(["生鲜采买"], fixed_stops=[chosen])
    )
    assert result["legs"][0]["entry_id"] == chosen["entry_id"]


@pytest.mark.parametrize("index", [-1, 3, True, "0"])
def test_invalid_fixed_station_index_is_rejected(trip_feature, trip_provider, index):
    feature = _choices(trip_feature)
    chosen = _pin(feature, 0, "生鲜采买", "指定生鲜超市")
    with pytest.raises(TripError, match="站序"):
        asyncio.run(
            TripService(trip_provider, feature, ORIGIN).plan(
                ["生鲜采买"], fixed_stops=[{**chosen, "index": index}]
            )
        )
    assert not trip_provider.test_hits


def test_duplicate_or_wrong_category_selections_are_rejected(trip_feature, trip_provider):
    feature = _choices(trip_feature)
    chosen = _pin(feature, 0, "生鲜采买", "指定生鲜超市")
    with pytest.raises(TripError, match="重复"):
        asyncio.run(
            TripService(trip_provider, feature, ORIGIN).plan(
                ["生鲜采买", "医药"], fixed_stops=[chosen, chosen]
            )
        )
    with pytest.raises(TripError, match="所选设施"):
        asyncio.run(
            TripService(trip_provider, feature, ORIGIN).plan(["医药"], fixed_stops=[chosen])
        )


def test_browsing_options_uses_no_baidu_or_trip_request_budget(
    trip_feature, trip_provider, tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "options-api.sqlite3")
    monkeypatch.setattr(main, "_config_warnings", lambda: [])
    feature = _choices(trip_feature)
    original = copy.deepcopy(feature)
    trip_provider._s = replace(trip_provider._s, trip_hour_requests=0, server_ak="")
    with TestClient(main.app) as client:
        main.app.state.baidu = trip_provider
        for _ in range(2):
            response = client.post(
                "/api/trip/options",
                json={"feature": feature, "origin": ORIGIN, "categories": ["生鲜采买", "基础教育"]},
            )
            assert response.status_code == 200
            groups = response.json()["groups"]
            assert len(groups[0]["items"]) == 2
            assert {node["gate"] for node in groups[1]["items"]} == {"东门", "西门"}
            assert all(node["straight_m"] >= 0 for group in groups for node in group["items"])
        assert not trip_provider.test_hits
    assert feature == original


def test_options_filter_closed_school_gate_and_do_not_include_pending_or_excluded_stores(
    trip_feature, trip_provider, tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "options-filter.sqlite3")
    monkeypatch.setattr(main, "_config_warnings", lambda: [])
    feature = _choices(trip_feature)
    school = feature["properties"]["coverage"]["places"][-1]
    feature["properties"]["closures"] = [
        {"lat": school["entries"][0]["lat"], "lng": school["entries"][0]["lng"], "radius_m": 20}
    ]
    feature["properties"]["coverage"]["places"] += [
        facility("待确认普通超市", 300, "生鲜采买"),
        facility("罗森便利店", 250, "生鲜采买"),
    ]
    # 离线快照保留这里的已知围挡，避免正式出行叠加共享证据时清除临时围挡。
    feature["properties"]["simulated"] = True
    with TestClient(main.app) as client:
        main.app.state.baidu = trip_provider
        response = client.post(
            "/api/trip/options",
            json={"feature": feature, "origin": ORIGIN, "categories": ["基础教育", "生鲜采买"]},
        )
        assert response.status_code == 200
        groups = response.json()["groups"]
        assert [n["gate"] for n in groups[0]["items"]] == ["西门"]
        assert {n["name"] for n in groups[1]["items"]} == {"较近菜市场", "指定生鲜超市"}
        assert not trip_provider.test_hits


def test_api_plans_the_facilities_and_gate_selected_from_options(
    trip_feature, trip_provider, tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "choice-plan-api.sqlite3")
    monkeypatch.setattr(main, "_config_warnings", lambda: [])
    feature = _choices(trip_feature)
    with TestClient(main.app) as client:
        main.app.state.baidu = trip_provider
        groups = client.post(
            "/api/trip/options",
            json={"feature": feature, "origin": ORIGIN, "categories": ["生鲜采买", "基础教育"]},
        ).json()["groups"]
        store = next(n for n in groups[0]["items"] if n["name"] == "指定生鲜超市")
        school = next(n for n in groups[1]["items"] if n["gate"] == "西门")
        fixed = [
            {"index": index, "place_id": node["place_id"], "entry_id": node["entry_id"]}
            for index, node in enumerate((store, school))
        ]
        response = client.post(
            "/api/trip/plan",
            json={
                "feature": feature,
                "origin": ORIGIN,
                "stops": ["生鲜采买", "基础教育"],
                "fixed_stops": fixed,
            },
        )
        assert response.status_code == 200
        result = response.json()
        assert [n["entry_id"] for n in result["legs"]] == [n["entry_id"] for n in fixed]
        assert result["legs"][1]["gate"] == "西门"


def test_six_stop_auto_plan_keeps_request_budget_bounded(trip_feature, trip_provider):
    feature = trip_feature
    feature["properties"]["coverage"]["places"] = [
        facility(f"{category.name}设施{j}", 200 + j * 100, category.name, index * 55)
        for index, category in enumerate(CATEGORIES)
        for j in range(8)
    ]
    stops = [category.name for category in CATEGORIES]
    result = asyncio.run(TripService(trip_provider, feature, ORIGIN).plan(stops))
    assert len(result["legs"]) == 6
    assert [leg["category"] for leg in result["legs"]] == stops
    assert result["quota"]["matrix_pairs"] <= trip_provider._s.trip_request_pairs
    matrix_calls = [p for endpoint, p in trip_provider.test_hits if "routematrix" in endpoint]
    counts = [
        len(p["origins"].split("|")) * len(p["destinations"].split("|")) for p in matrix_calls
    ]
    assert counts[:6] == [4, 16, 16, 16, 16, 16]


def test_six_fixed_stops_support_api_planning_and_location_reanchoring(
    trip_feature, trip_provider, tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "six-stop-api.sqlite3")
    monkeypatch.setattr(main, "_config_warnings", lambda: [])
    feature = trip_feature
    stops = [category.name for category in CATEGORIES]
    places = [
        facility(f"{name}设施", 200 + index * 80, name, index * 50)
        for index, name in enumerate(stops)
    ]
    feature["properties"]["coverage"]["places"] = places
    chosen = [_pin(feature, i, name, places[i]["name"]) for i, name in enumerate(stops)]
    with TestClient(main.app) as client:
        main.app.state.baidu = trip_provider
        response = client.post(
            "/api/trip/plan",
            json={"feature": feature, "origin": ORIGIN, "stops": stops, "fixed_stops": chosen},
        )
        assert response.status_code == 200
        assert len(response.json()["legs"]) == 6
        response = client.post(
            "/api/trip/reanchor",
            json={
                "feature": feature,
                "origin": {**ORIGIN, "kind": "map"},
                "stops": stops,
                "selected_stops": [
                    {"place_id": n["place_id"], "entry_id": n["entry_id"]} for n in chosen
                ],
                "places": places,
            },
        )
        assert response.status_code == 200
        assert [n["entry_id"] for n in response.json()["legs"]] == [n["entry_id"] for n in chosen]
        assert (
            client.post(
                "/api/trip/plan",
                json={"feature": feature, "origin": ORIGIN, "stops": [*stops, "医药"]},
            ).status_code
            == 400
        )
