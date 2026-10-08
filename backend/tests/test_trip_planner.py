import asyncio
import itertools
import random

import pytest

from app.trip.candidates import Node, lower_bound
from app.trip.planner import combinations_by_bound, solve
from app.trip.service import TripService


def test_layered_dp_matches_exhaustive_search_and_lazy_bounds_are_ordered():
    rng = random.Random(42)
    origin = Node("o", "o", "起点", "", 31.25, 121.42)
    layers = [
        [
            Node(f"{k}-{j}", f"{k}-{j}", str(j), str(k), 31.25 + k * 0.001, 121.42 + j * 0.001)
            for j in range(3)
        ]
        for k in range(3)
    ]
    edges = {}
    previous = [origin]
    for layer in layers:
        for a in previous:
            for b in layer:
                edges[a.id, b.id] = {"distance_m": rng.randint(100, 600)}
        previous = layer
    brute = min(
        sum(edges[a.id, b.id]["distance_m"] for a, b in zip([origin, *p], p, strict=False))
        for p in itertools.product(*layers)
    )
    assert solve(origin, layers, edges)[0] == brute
    combos = list(combinations_by_bound(origin, layers, 30, 1000))
    assert len(combos) == 27
    assert [b for b, _ in combos] == sorted(b for b, _ in combos)
    for b, p in combos:
        assert b == pytest.approx(
            sum(lower_bound(a, t, 30) for a, t in zip([origin, *p], p, strict=False))
        )


def test_total_slack_is_per_edge_not_best_minus_slack():
    origin = Node("o", "o", "起点", "", 31.25, 121.42)
    a = Node("a", "a", "a", "", 31.254, 121.42)
    b = Node("b", "b", "b", "", 31.258, 121.42)
    summed = sum(lower_bound(x, y, 30) for x, y in [(origin, a), (a, b)])
    assert summed < 850  # 两段共减 60 米；仅用 best−30 会漏掉仍可能更短的组合。


def test_tied_lower_bounds_cannot_grow_an_unbounded_prefix_queue(monkeypatch):
    from app.trip import planner

    origin = Node("o", "o", "起点", "", 31.25, 121.42)
    layers = [
        [Node(f"{k}-{j}", f"{k}-{j}", "同下界设施", "", 31.25, 121.42) for j in range(10)]
        for k in range(3)
    ]
    original_push = planner.heapq.heappush
    pushed = 0

    def count_push(queue, item):
        nonlocal pushed
        pushed += 1
        return original_push(queue, item)

    monkeypatch.setattr(planner.heapq, "heappush", count_push)
    with pytest.raises(RuntimeError, match="计算达到上限"):
        list(combinations_by_bound(origin, layers, 30, max_expansions=25))
    assert pushed <= 24


def test_plan_respects_order_and_manual_change_keeps_neighbours(trip_feature, trip_provider):
    from test_trip_candidates import CENTER, facility

    trip_feature["properties"]["coverage"]["places"] = [
        facility(f"{category}{j}", 200 + j * 100, category, k * 70)
        for k, category in enumerate(["生鲜采买", "医药", "基础教育"])
        for j in range(3)
    ]
    origin = {"lat": CENTER[0], "lng": CENTER[1], "kind": "center"}
    stops = ["生鲜采买", "医药", "基础教育"]
    result = asyncio.run(TripService(trip_provider, trip_feature, origin).plan(stops))
    assert [leg["category"] for leg in result["legs"]] == stops
    assert result["optimality"] in ("snapshot_tolerance", "snapshot_measured")
    alt = result["alternatives"][1]["items"][0]
    selected = [
        {"entry_id": leg["entry_id"], "place_id": leg["place_id"]} for leg in result["legs"]
    ]
    changed = asyncio.run(
        TripService(trip_provider, trip_feature, origin).plan(stops, selected, {"index": 1, **alt})
    )
    assert changed["legs"][0]["entry_id"] == result["legs"][0]["entry_id"]
    assert changed["legs"][2]["entry_id"] == result["legs"][2]["entry_id"]
    assert changed["total_m"] == alt["total_m"]
    assert changed["optimality"] == "manual"


def test_simulated_plan_calls_no_baidu(trip_feature, trip_provider):
    trip_feature["properties"]["simulated"] = True
    del trip_feature["properties"]["coverage"]["places"]
    result = asyncio.run(
        TripService(trip_provider, trip_feature, {"lat": 31.25, "lng": 121.42}).plan(
            ["生鲜采买", "基础教育"]
        )
    )
    assert result["basis"] == "estimate" and result["legs"]
    assert not trip_provider.test_hits


@pytest.mark.parametrize("budget,seed", [(120, [6, 36, 36]), (50, [4, 16, 16])])
def test_three_stop_seed_and_gate_continuity_fit_the_actual_pair_budget(
    trip_feature, trip_provider, budget, seed
):
    from dataclasses import replace

    from test_trip_candidates import CENTER, facility

    trip_provider._s = replace(trip_provider._s, trip_request_pairs=budget)
    places = []
    for j in range(8):
        places.append(facility(f"菜市场{j}", 100 + j * 100, "生鲜采买", 0))
        school = facility(f"学{j}", 300 + j * 100, "基础教育", 35)
        school["entries"] = [
            {**facility(f"门{j}-{k}", 300 + j * 100 + k * 30, "基础教育", 35), "name": f"门{k}"}
            for k in range(4)
        ]
        places.append(school)
        places.append(facility(f"药{j}", 200 + j * 100, "医药", 70))
    trip_feature["properties"]["coverage"]["places"] = places
    result = asyncio.run(
        TripService(trip_provider, trip_feature, {"lat": CENTER[0], "lng": CENTER[1]}).plan(
            ["生鲜采买", "基础教育", "医药"]
        )
    )
    assert len(result["legs"]) == 3
    assert result["legs"][2]["from"]["lat"] == result["legs"][1]["lat"]
    assert result["legs"][2]["from"]["lng"] == result["legs"][1]["lng"]
    pairs = [
        len(p["origins"].split("|")) * len(p["destinations"].split("|"))
        for endpoint, p in trip_provider.test_hits
        if "routematrix" in endpoint
    ]
    assert pairs[:3] == seed
    assert max(pairs) <= 50 and sum(pairs) <= budget
