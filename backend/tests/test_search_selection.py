import asyncio

import httpx
import pytest
from fastapi import HTTPException

from app.baidu.client import BaiduMapClient
from app.config import Settings
from app.main import IsochroneRequest, _resolve_center, app


def test_search_candidates_keep_coordinates_scope_and_cached_city_separate(tmp_path, monkeypatch):
    client = BaiduMapClient(Settings(server_ak="test", browser_ak="", cache_dir=tmp_path))
    hits = []

    async def request(endpoint, params):
        hits.append((endpoint, params))
        place = {
            "uid": "poi-one",
            "name": "长风大悦城",
            "city": "上海市",
            "district": "普陀区",
            "address": "大渡河路",
            "location": {"lat": 31.22, "lng": 121.39},
        }
        return {
            "result": [
                place,
                place,
                {"name": "无位置"},
                {"name": "错误坐标", "location": {"lat": 999, "lng": 121}},
            ]
        }

    monkeypatch.setattr(client, "_request", request)

    async def check():
        a = await client.place_suggestions("长风大悦城", "上海市")
        assert a == await client.place_suggestions("长风大悦城", "上海市")
        assert len(a) == 1 and a[0]["lat"] == 31.22 and a[0]["address"] == "大渡河路"
        await client.place_suggestions("长风大悦城", "全国")

    asyncio.run(check())
    assert len(hits) == 2
    assert hits[0][1]["city_limit"] == "true" and hits[1][1]["city_limit"] == "false"


def test_candidate_endpoint_and_empty_search_do_not_silently_geocode(monkeypatch):
    class Net:
        async def place_suggestions(self, query, city):
            assert (query, city) == ("长风大悦城", "上海市")
            return []

    monkeypatch.setattr(app.state, "baidu", Net(), raising=False)

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as net:
            r = await net.get(
                "/api/places/suggestions", params={"query": "长风大悦城", "city": "上海市"}
            )
            assert r.status_code == 200 and r.json()["candidates"] == []
            r = await net.get("/api/places/suggestions", params={"query": "  ", "city": "上海市"})
            assert r.status_code == 422

    asyncio.run(check())


def test_selected_place_cannot_label_another_coordinate():
    selection = {
        "name": "长风大悦城",
        "query": "长风",
        "city": "上海市",
        "lat": 31.22,
        "lng": 121.39,
    }
    req = IsochroneRequest(lat=31.22, lng=121.39, search_selection=selection)
    assert asyncio.run(_resolve_center(None, req)) == (31.22, 121.39)
    req = IsochroneRequest(lat=30.57, lng=117.23, search_selection=selection)
    with pytest.raises(HTTPException) as err:
        asyncio.run(_resolve_center(None, req))
    assert err.value.status_code == 422
