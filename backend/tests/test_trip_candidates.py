import asyncio
import copy
import random

import pytest

from app.isochrone.geometry import offset_point
from app.trip.candidates import candidates, place_id, prepare, search_status
from app.trip.service import TripService

CENTER = (31.25, 121.42)


def facility(name, meters, category="医药", bearing=0):
    lat, lng = offset_point(*CENTER, bearing, meters)
    return {"name": name, "lat": lat, "lng": lng, "category": category, "in_circle": True}


def test_gates_are_distinct_and_closed_gates_do_not_remove_the_school(trip_feature):
    school = facility("学校", 400, "基础教育")
    school["entries"] = [
        {**facility("北门", 300), "name": "北门"},
        {**facility("南门", 500), "name": "南门"},
    ]
    trip_feature["properties"]["coverage"]["places"] = [school]
    closure = {
        "lat": school["entries"][0]["lat"],
        "lng": school["entries"][0]["lng"],
        "radius_m": 20,
    }
    nodes = candidates(trip_feature, "基础教育", [closure])
    assert len(nodes) == 1 and nodes[0].gate == "南门"
    assert nodes[0].place_id == place_id(school)


@pytest.mark.parametrize("seed", range(5))
def test_nearest_matches_all_candidates_even_when_walk_is_shorter_than_straight(
    trip_feature, trip_provider, seed
):
    rng = random.Random(seed)
    places = [facility(f"药店{i}", 100 + i * 20) for i in range(25)]
    trip_feature["properties"]["coverage"]["places"] = places
    from app.trip.candidates import point

    costs = {point(p): 100 + i * 20 + rng.uniform(-20, 170) for i, p in enumerate(places)}

    def answer(endpoint, params):
        if "routematrix" in endpoint:
            dests = [tuple(map(float, p.split(","))) for p in params["destinations"].split("|")]
            return {
                "status": 0,
                "result": [
                    {"distance": {"value": costs[p]}, "duration": {"value": costs[p] / 1.17}}
                    for p in dests
                ],
            }

    trip_provider.test_answer = answer
    result = asyncio.run(
        TripService(
            trip_provider, trip_feature, {"lat": CENTER[0], "lng": CENTER[1], "kind": "center"}
        ).nearest("医药", 5)
    )
    expected = sorted(places, key=lambda p: costs[point(p)])[:5]
    assert [i["name"] for i in result["items"]] == [p["name"] for p in expected]
    assert result["ranking_verified"]
    assert result["quota"]["matrix_pairs"] < len(places)


def test_origin_range_and_unknown_search_are_separate(trip_feature):
    trip_feature["properties"]["coverage"]["radius_m"] = 1000
    lat, lng = offset_point(*CENTER, 0, 1001)
    with pytest.raises(ValueError, match="太远"):
        prepare(trip_feature, {"lat": lat, "lng": lng})
    assert search_status(trip_feature, ["医药"]) == "unknown"
    trip_feature["properties"]["coverage"]["search_metadata"] = {
        "医药": [{"complete": False, "truncated": True}]
    }
    assert search_status(trip_feature, ["医药"]) == "truncated"


def test_bad_slack_does_not_claim_lower_bound_proof(trip_feature, trip_provider):
    trip_feature["properties"]["coverage"]["places"] = [
        facility(f"药店{i}", 100 + i * 30) for i in range(12)
    ]

    def answer(endpoint, params):
        if "routematrix" in endpoint:
            return {
                "status": 0,
                "result": [
                    {"distance": {"value": 20}, "duration": {"value": 20}}
                    for p in params["destinations"].split("|")
                ],
            }

    trip_provider.test_answer = answer
    result = asyncio.run(
        TripService(
            trip_provider, copy.deepcopy(trip_feature), {"lat": CENTER[0], "lng": CENTER[1]}
        ).nearest("医药", 5)
    )
    assert result["verification_basis"] == "all_snapshot_candidates"
    assert result["quota"]["matrix_pairs"] == 12
    assert any("偏差" in w for w in result["warnings"])
