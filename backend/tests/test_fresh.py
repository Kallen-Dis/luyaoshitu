"""证据分级、保守盲区判定、用户修正和按店名补查的回归。全程假传输。"""

import asyncio
import copy

import pytest
from test_blindspot import CENTER, FakeClient, _coverage
from test_markings_apply import mk
from test_trip_candidates import facility

from app.main import _current
from app.markings.apply import modify_coverage, plan
from app.markings.models import MarkingValidationError, normalize
from app.markings.routes import MarkingIn
from app.poi.catalog import by_name
from app.poi.collect import Poi, collect_category
from app.poi.fresh import FRESH, VERSION, annotate_coverage, classify, eligible
from app.report.blindspot import BlindspotConfig, CellResult, _judge_category
from app.report.score import build_report
from app.trip.service import TripService


@pytest.mark.parametrize(
    "name,status",
    [
        ("振凯平价生鲜大卖场", "inferred"),
        ("文华生鲜超市", "inferred"),
        ("曹杨农贸市场", "inferred"),
        ("集鲜菜市", "inferred"),
        ("世纪联华武宁店", "inferred"),
        ("联华超市", "pending"),
        ("华联超市", "pending"),
        ("联华便利超市", "excluded"),
        ("生鲜水果专卖", "excluded"),
        ("桂巷驿站", "excluded"),
        ("良友便利店", "excluded"),
        ("蔬菜驿站", "inferred"),
        ("生鲜超市设备", "excluded"),
        ("果蔬生鲜", "inferred"),
        ("盒马鲜生", "inferred"),
        ("百货购物中心", "excluded"),
        ("某商场内生鲜超市", "inferred"),
        ("某食品店", "pending"),
    ],
)
def test_classification_never_treats_brand_or_supermarket_tag_as_inventory(name, status):
    result = classify(name, "购物;超市")
    assert result["fresh_status"] == status
    assert result["fresh_evidence"]
    assert classify(name, status="verified", evidence="现场照片") == {
        "fresh_status": "verified",
        "fresh_evidence": "现场照片",
        "fresh_rule_version": VERSION,
    }


@pytest.mark.parametrize(
    "walk,unknown,reason",
    [
        (800, True, "fresh_pending"),
        (1200, False, None),
        (None, True, "fresh_pending_distance_unknown"),
    ],
)
def test_only_pending_supermarket_is_not_asserted_as_covered_or_missing(walk, unknown, reason):
    shop = Poi("联华超市", CENTER[0] + 0.001, CENTER[1], FRESH)
    cell = CellResult(*CENTER)
    fake = FakeClient({shop.name: walk}).register([shop])
    asyncio.run(
        _judge_category(fake, [cell], by_name(FRESH), _coverage(FRESH, [shop]), BlindspotConfig())
    )
    assert (FRESH in cell.unknown) == unknown
    assert (FRESH in cell.missing) == (not unknown)
    assert cell.unknown_reasons.get(FRESH) == reason
    if unknown:
        assert cell.nearest_m[FRESH] is None


def test_accepted_reachable_shop_does_not_measure_pending_supermarket():
    fresh = Poi("菜市场", CENTER[0] + 0.002, CENTER[1], FRESH)
    pending = Poi("联华超市", CENTER[0] + 0.001, CENTER[1], FRESH)
    cell = CellResult(*CENTER)
    fake = FakeClient({fresh.name: 800, pending.name: 500}).register([fresh, pending])
    asyncio.run(
        _judge_category(
            fake, [cell], by_name(FRESH), _coverage(FRESH, [fresh, pending]), BlindspotConfig()
        )
    )
    assert cell.missing == cell.unknown == []
    assert cell.nearest_m[FRESH] == 800
    assert fake.pairs == 1


def test_pending_count_is_separate_and_not_scored_as_absence():
    coverage = {"places": [facility("联华超市", 100, FRESH)], "categories": {FRESH: 1, "医药": 1}}
    annotate_coverage(coverage)
    assert coverage["categories"][FRESH] == 0
    assert coverage["fresh_breakdown"]["pending_in_circle"] == 1
    report = build_report({}, coverage)
    assert FRESH not in report["blinds"]
    assert report["uncertain_categories"] == [FRESH]
    assert report["dimensions"]["cover"] == 100


@pytest.mark.parametrize("shop_name", ["联华超市", "百货购物中心"])
def test_legacy_grid_is_unknown_until_recomputed_but_new_rules_are_preserved(shop_name):
    props = {
        "coverage": {"places": [facility(shop_name, 100, FRESH)]},
        "blindspots": {
            "layout": "polygon",
            "cells": [
                {
                    "lat": 31.25,
                    "lng": 121.42,
                    "missing": [FRESH],
                    "unknown": [],
                    "nearest_m": {FRESH: 500},
                }
            ],
            "blind_ratio": {FRESH: 1},
            "blind_count": 1,
        },
    }
    current = _current(props)
    assert current["blindspots"]["cells"][0]["unknown_reasons"][FRESH] == "fresh_legacy"
    assert current["blindspots"]["blind_count"] == 0
    assert FRESH not in current["blindspots"]["blind_ratio"]
    assert FRESH not in current["report"]["blinds"]
    assert current["report"]["uncertain_categories"] == [FRESH]
    new = copy.deepcopy(props)
    new["blindspots"]["fresh_rule_version"] = VERSION
    new["blindspots"]["cells"][0]["unknown_reasons"][FRESH] = "fresh_pending"
    assert _current(new)["blindspots"]["cells"][0]["unknown_reasons"][FRESH] == "fresh_pending"


def test_confirmation_upgrades_existing_shop_without_duplicate_and_obeys_trust_policy():
    shop = Poi("联华超市", 31.25, 121.42, FRESH)
    coverage = _coverage(FRESH, [shop])
    mark = mk(
        1,
        "facility_extra",
        {
            "category": FRESH,
            "name": shop.name,
            "lat": shop.lat,
            "lng": shop.lng,
            "sells_vegetables": True,
        },
        mine=True,
    )
    result = modify_coverage(coverage, plan([mark], "auto"))
    assert len(result.pois_of(FRESH)) == 1
    assert result.pois_of(FRESH)[0].fresh_status == "verified"
    assert eligible(result.pois_of(FRESH)[0])
    assert not eligible(shop)  # 原始 POI 未被修改。
    mark["mine"] = False
    assert not eligible(modify_coverage(coverage, plan([mark], "auto")).pois_of(FRESH)[0])


def test_confirmation_survives_http_model_and_rejects_nonfresh_or_false():
    data = {
        "type": "facility_extra",
        "category": FRESH,
        "name": "联华超市",
        "lat": 31.25,
        "lng": 121.42,
        "sells_vegetables": True,
    }
    spec, _ = normalize(MarkingIn(**data).model_dump())
    assert spec["sells_vegetables"] is True
    for patch in ({"sells_vegetables": False}, {"category": "医药"}):
        with pytest.raises(MarkingValidationError):
            normalize({**data, **patch})
    old = MarkingIn(**{k: v for k, v in data.items() if k != "sells_vegetables"})
    assert "sells_vegetables" not in normalize(old.model_dump())[0]


def test_negative_confirmation_remains_applied_even_before_named_recall():
    mark = mk(
        1,
        "facility_missing",
        {
            "category": FRESH,
            "name": "联华超市",
            "lat": 31.25,
            "lng": 121.42,
            "reason": "no_vegetables",
        },
        mine=True,
    )
    selected = plan([mark], "auto")
    modify_coverage(_coverage(FRESH, []), selected)
    assert selected.applied == [mark]


def test_trip_default_excludes_pending_but_opt_in_measures_and_keeps_badge(
    trip_provider, trip_feature
):
    fresh = facility("生鲜超市", 400, FRESH)
    pending = facility("联华超市", 100, FRESH)
    trip_feature["properties"]["coverage"]["places"] = [fresh, pending]
    origin = {"lat": 31.25, "lng": 121.42, "kind": "center"}

    def query(include):
        return asyncio.run(
            TripService(trip_provider, copy.deepcopy(trip_feature), origin).nearest(
                FRESH, 5, include_pending=include
            )
        )

    result = query(False)
    assert [p["name"] for p in result["items"]] == [fresh["name"]]
    assert result["pending_count"] == 1
    assert result["pending_candidates"][0]["name"] == pending["name"]
    assert "walk_m" not in result["pending_candidates"][0]
    result = query(True)
    assert result["items"][0]["name"] == pending["name"]
    assert result["items"][0]["fresh_status"] == "pending"


def test_named_lookup_details_range_cache_and_live_collection(trip_provider, trip_feature):
    shop = facility("振凯平价生鲜大卖场", 150, FRESH)
    far = facility("振凯生鲜远店", 4000, FRESH)

    def answer(endpoint, params):
        if "reverse_geocoding" in endpoint:
            return {"status": 0, "result": {"addressComponent": {"city": "上海市"}}}
        if "suggestion" in endpoint:
            return {
                "status": 0,
                "result": [
                    {"name": p["name"], "uid": uid} for p, uid in [(shop, "near"), (far, "far")]
                ]
                + [{"name": "振凯路", "uid": "street"}],
            }
        if "detail" in endpoint:
            p = {"near": shop, "far": far, "street": {**shop, "name": "振凯路"}}[params["uid"]]
            return {
                "status": 0,
                "result": {
                    "name": p["name"],
                    "location": {"lat": p["lat"], "lng": p["lng"]},
                    "detail_info": {"tag": "购物;超市" if params["uid"] != "street" else "道路"},
                },
            }
        return None

    trip_provider.test_answer = answer
    origin = {"lat": 31.2501, "lng": 121.42, "kind": "map"}

    def query(name=None):
        return asyncio.run(
            TripService(trip_provider, copy.deepcopy(trip_feature), origin).nearest(
                FRESH, 5, poi_query=name
            )
        )

    assert query("振凯")["items"][0]["name"] == shop["name"]
    assert query()["items"][0]["name"] == shop["name"]
    before = len(trip_provider.test_hits)
    query("振凯")
    assert len(trip_provider.test_hits) == before
    poi_hits = [p for e, p in trip_provider.test_hits if "suggestion" in e]
    assert poi_hits == [
        {"query": "振凯", "region": "上海市", "city_limit": "true", "output": "json"}
    ]
    collected = asyncio.run(collect_category(trip_provider, by_name(FRESH), (31.25, 121.42), 2500))
    assert [p.name for p in collected.pois] == [shop["name"]]
    assert not trip_feature["properties"]["coverage"]["places"]  # 查询输入未变。
    assert not list((trip_provider._s.cache_dir / "trip").rglob("*.json"))


def test_failed_named_lookup_preserves_existing_recommendation(trip_provider, trip_feature):
    trip_feature["properties"]["coverage"]["places"] = [facility("菜市场", 300, FRESH)]
    trip_provider.test_answer = lambda e, p: {"status": 302} if "reverse_geocoding" in e else None
    result = asyncio.run(
        TripService(trip_provider, trip_feature, {"lat": 31.25, "lng": 121.42}).nearest(
            FRESH, 5, poi_query="振凯"
        )
    )
    assert result["items"][0]["name"] == "菜市场"
    assert any("未完成" in warning for warning in result["warnings"])


def test_pending_supermarket_near_pruning_boundary_is_still_measured():
    from app.isochrone.geometry import offset_point

    lat, lng = offset_point(*CENTER, 0, 1015)
    shop = Poi("联华超市", lat, lng, FRESH)
    cell = CellResult(*CENTER)
    fake = FakeClient({shop.name: 995}).register([shop])
    asyncio.run(
        _judge_category(fake, [cell], by_name(FRESH), _coverage(FRESH, [shop]), BlindspotConfig())
    )
    assert fake.pairs == 1
    assert cell.unknown_reasons[FRESH] == "fresh_pending"
    assert not cell.missing


def test_uncollected_fresh_category_is_not_invented_as_zero():
    coverage = {"places": [], "categories": {"医药": 1}}
    annotate_coverage(coverage)
    assert coverage["categories"] == {"医药": 1}
    assert "fresh_breakdown" not in coverage
