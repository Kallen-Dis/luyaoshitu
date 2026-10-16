"""区域水域缓存与海陆重建，全部使用人工 OSM 响应，不访问网络。"""

import asyncio
import json

import httpx
import pytest
from shapely.geometry import LineString, Point, box

from app.isochrone import osmwater as osm
from app.isochrone.osmcoords import to_bd, to_wgs


def response(elements=()):
    return {"osm3s": {"timestamp_osm_base": "2026-10-16T00:00:00Z"}, "elements": list(elements)}


def way(points, **tags):
    return {
        "type": "way",
        "id": 1,
        "tags": tags,
        "geometry": [{"lon": x, "lat": y} for x, y in points],
    }


@pytest.fixture(autouse=True)
def reset_gate(monkeypatch):
    monkeypatch.setattr(osm, "_lock", asyncio.Lock())
    monkeypatch.setattr(osm, "_last_request", -1000)


def test_directed_coast_separates_land_from_sea():
    frame = box(0, 0, 10, 10)
    sea = osm.sea_polygon([LineString([(5, -1), (5, 11)])], frame)
    assert sea.covers(Point(8, 5)) and not sea.covers(Point(2, 5))
    assert sea.area == pytest.approx(50)


def test_closed_island_has_water_outside_and_land_inside():
    coast = LineString([(3, 3), (7, 3), (7, 7), (3, 7), (3, 3)])
    sea = osm.sea_polygon([coast], box(0, 0, 10, 10))
    assert sea.covers(Point(1, 1)) and not sea.covers(Point(5, 5))


def test_broken_coast_is_rejected_instead_of_closing_it_with_a_chord():
    with pytest.raises(ValueError):
        osm.sea_polygon([LineString([(5, 2), (5, 8)])], box(0, 0, 10, 10))


def test_multipolygon_lake_preserves_inner_island():
    r = {
        "members": [
            {"type": "way", "role": "outer", **way([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)])},
            {"type": "way", "role": "inner", **way([(4, 4), (6, 4), (6, 6), (4, 6), (4, 4)])},
        ]
    }
    water = osm.relation_polygon(r)
    assert water.area == pytest.approx(96) and not water.covers(Point(5, 5))
    r["members"][1].pop("geometry")
    with pytest.raises(ValueError):
        osm.relation_polygon(r)


def test_water_relation_without_islands_accepts_empty_inner_ring():
    r = {
        "members": [
            {"type": "way", "role": "outer", **way([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)])}
        ]
    }
    assert osm.relation_polygon(r).area == pytest.approx(100)


@pytest.mark.parametrize("lat,lng", [(31.23, 121.41), (29.97, 122.33), (39.9, 116.4)])
def test_local_conversion_roundtrip(lat, lng):
    x, y = to_bd(lng, lat)
    a, b = to_wgs(y, x)
    # 这是换算内部一致性，不能作为外部对齐准确度证明。
    assert abs(lat - a) < 2e-6 and abs(lng - b) < 2e-6


def test_nearby_points_share_region_cache_key():
    assert osm.region((31.234, 121.412), 2400) == osm.region((31.23401, 121.41201), 2400)


def test_valid_response_cached_once_and_partial_response_never_cached(tmp_path):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json=response())

    async def check():
        transport = httpx.MockTransport(handler)
        a, meta = await osm.resolve_osm_domain(
            (31.23, 121.41), 2400, cache_dir=tmp_path, transport=transport
        )
        b, cached = await osm.resolve_osm_domain(
            (31.23, 121.41), 2400, cache_dir=tmp_path, transport=transport
        )
        assert a and b and not meta["cache_hit"] and cached["cache_hit"]
        assert meta["data_sha256"] == cached["data_sha256"]
        assert not meta["alignment_verified"] and len(calls) == 1

    asyncio.run(check())


@pytest.mark.parametrize(
    "body",
    [
        {"remark": "runtime error", **response()},
        {},
        response([way([(0, 0), (1, 0)], natural="water")]),
    ],
)
def test_invalid_responses_are_failures_with_cooldown(tmp_path, body):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json=body)

    async def check():
        kwargs = dict(cache_dir=tmp_path, transport=httpx.MockTransport(handler))
        first, status = await osm.resolve_osm_domain((31.23, 121.41), 2400, **kwargs)
        second, cooldown = await osm.resolve_osm_domain((31.23, 121.41), 2400, **kwargs)
        assert first is second is None and status["status"] == "unavailable"
        assert "冷却" in cooldown["reason"] and len(calls) == 1
        assert len(list(tmp_path.glob("*.failure.json"))) == 1

    asyncio.run(check())


def test_cached_raw_response_is_auditable_json(tmp_path):
    body = response()

    async def check():
        await osm.resolve_osm_domain(
            (31.23, 121.41),
            2400,
            cache_dir=tmp_path,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
        )

    asyncio.run(check())
    paths = list(tmp_path.glob("*.json"))
    assert len(paths) == 1 and json.loads(paths[0].read_text()) == body


def test_gateway_timeout_uses_backup_without_claiming_no_water(tmp_path):
    calls = []

    def handler(req):
        calls.append(str(req.url.host))
        return httpx.Response(504) if len(calls) == 1 else httpx.Response(200, json=response())

    domain, meta = asyncio.run(
        osm.resolve_osm_domain(
            (31.23, 121.41), 2400, cache_dir=tmp_path, transport=httpx.MockTransport(handler)
        )
    )
    assert domain is not None and meta["service_attempts"] == 2
    assert calls[0] != calls[1]


def test_rate_limit_does_not_switch_service(tmp_path):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(429)

    domain, meta = asyncio.run(
        osm.resolve_osm_domain(
            (31.23, 121.41), 2400, cache_dir=tmp_path, transport=httpx.MockTransport(handler)
        )
    )
    assert domain is None and len(calls) == 1 and "429" in meta["detail"]


def test_expired_but_valid_cache_keeps_known_water_during_failure_and_cooldown(tmp_path):
    import os
    import time

    async def check():
        await osm.resolve_osm_domain(
            (31.23, 121.41),
            2400,
            cache_dir=tmp_path,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response())),
        )
        cache = next(tmp_path.glob("*.json"))
        os.utime(cache, (time.time() - osm.TTL - 1,) * 2)
        failed = httpx.MockTransport(lambda _: httpx.Response(504))
        a, m = await osm.resolve_osm_domain(
            (31.23, 121.41), 2400, cache_dir=tmp_path, transport=failed
        )
        b, n = await osm.resolve_osm_domain(
            (31.23, 121.41), 2400, cache_dir=tmp_path, transport=failed
        )
        assert a is not None and b is not None and m["stale"] and n["stale"]

    asyncio.run(check())
