import asyncio
import copy

from test_trip_candidates import CENTER, facility

from app.trip.service import TripService


def run(provider, feature, origin=None):
    return asyncio.run(
        TripService(
            provider,
            copy.deepcopy(feature),
            origin
            or {
                "lat": CENTER[0],
                "lng": CENTER[1],
                "kind": "center",
            },
        ).nearest("生鲜采买", 5)
    )


def test_missing_fresh_shop_is_recalled_ranked_and_deduplicated(trip_provider, trip_feature):
    old = facility("原生鲜店", 600, category="生鲜采买")
    new = facility("平价生鲜大卖场", 150, category="生鲜采买")
    trip_feature["properties"]["coverage"]["places"] = [old]
    records = [
        {"name": p["name"], "location": {"lat": p["lat"], "lng": p["lng"]}} for p in (old, new)
    ]
    trip_provider.test_answer = (
        lambda e, p: {
            "status": 0,
            "results": records,
            "total": 2,
        }
        if "/place/v2/search" in e
        else None
    )
    origin = {"lat": CENTER[0] + 0.0001, "lng": CENTER[1], "kind": "map"}
    result = run(trip_provider, trip_feature, origin)
    assert [p["name"] for p in result["items"]] == [new["name"], old["name"]]
    assert result["search_status"] == "unknown"  # 补查不能替旧快照证明原始关键词完整。
    assert any("补充检索到 1 家" in w for w in result["warnings"])
    assert all(
        p["location"] == f"{CENTER[0]:.6f},{CENTER[1]:.6f}"
        for e, p in trip_provider.test_hits
        if "/place/v2/search" in e
    )
    assert not list((trip_provider._s.cache_dir / "trip").rglob("*.json"))
    hits = len(trip_provider.test_hits)
    run(trip_provider, trip_feature, origin)
    assert len(trip_provider.test_hits) == hits
    assert trip_feature["properties"]["coverage"]["places"] == [old]


def test_supplement_does_not_reintroduce_reported_closed_shop(trip_provider, trip_feature):
    shop = facility("已关门", 100, category="生鲜采买")
    trip_feature["properties"]["markings"] = {
        "applied": [
            {
                "type": "facility_missing",
                "spec": shop,
            }
        ]
    }
    trip_provider.test_answer = (
        lambda e, p: {
            "status": 0,
            "results": [{"name": shop["name"], "location": shop}],
            "total": 1,
        }
        if "/place/v2/search" in e
        else None
    )
    assert not run(trip_provider, trip_feature)["items"]


def test_failed_supplement_keeps_snapshot_and_reports_possible_omission(
    trip_provider, trip_feature
):
    shop = facility("原生鲜店", 600, category="生鲜采买")
    trip_feature["properties"]["coverage"]["places"] = [shop]
    trip_provider.test_answer = (
        lambda e, p: {
            "status": 302,
            "message": "配额用完",
        }
        if "/place/v2/search" in e
        else None
    )
    result = run(trip_provider, trip_feature)
    assert result["items"][0]["name"] == shop["name"]
    assert any("可能遗漏" in w for w in result["warnings"])


def test_actual_route_keeps_every_bend_and_converts_coordinates_once(trip_provider, trip_feature):
    shop = facility("原店", 600)
    trip_feature["properties"]["coverage"]["places"] = [shop]
    path = "121.42,31.25;121.4202,31.2501;121.4203,31.2505;121.4201,31.251"
    trip_provider.test_answer = (
        lambda e, p: {
            "status": 0,
            "result": {
                "routes": [
                    {
                        "distance": 650,
                        "duration": 700,
                        "steps": [{"path": path, "instruction": "沿道路步行"}],
                    }
                ]
            },
        }
        if "directionlite" in e
        else None
    )
    result = asyncio.run(
        TripService(
            trip_provider,
            copy.deepcopy(trip_feature),
            {
                "lat": CENTER[0],
                "lng": CENTER[1],
                "kind": "center",
            },
        ).nearest("医药", 5)
    )
    assert result["items"][0]["route"]["path"] == [
        [121.42, 31.25],
        [121.4202, 31.2501],
        [121.4203, 31.2505],
        [121.4201, 31.251],
    ]
