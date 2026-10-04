"""AI 二次核对（Agent Plan）：只给线索不改结论，问过的不再问，失败不当成「没有」，Token 不外泄。"""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import storage
from app.config import Settings
from app.isochrone.geometry import haversine_m, offset_point
from app.poi import crosscheck as cc
from app.samples import load_sample

REGION = "上海市普陀区"


def _caoyang():
    return main._prepared(load_sample("isochrone-caoyang-15min"))


def _gcj(lat, lng):
    glat, glng = cc.bd09_to_gcj02(lat, lng)
    return {"lat": glat, "lng": glng}


def _answer(feature):
    """一份模拟的 Agent Plan 回答：已收录的小学、新的小学、远处的小学、子点、托管班各一条。"""
    gap = cc.gaps_of(feature)[0]
    known = next(
        p for p in feature["properties"]["coverage"]["places"] if p["category"] == "基础教育"
    )
    near = offset_point(gap.cells[0][0], gap.cells[0][1], 90, 300)
    far = offset_point(gap.anchor[0], gap.anchor[1], 0, 5000)
    school = {"tag": "教育培训;小学"}
    return {
        "status": 0,
        "results": [
            {
                "name": known["name"],
                "location": _gcj(known["lat"], known["lng"]),
                "detail_info": school,
            },
            {"name": "测试新开小学", "location": _gcj(*near), "detail_info": school},
            {"name": "远处的小学", "location": _gcj(*far), "detail_info": school},
            {
                "name": "测试新开小学-东门",
                "location": _gcj(*near),
                "detail_info": {"tag": "出入口;门", "parent_id": "abc"},
            },
            {
                "name": "阳光小学托管班",
                "location": _gcj(*near),
                "detail_info": {"tag": "教育培训;托管班"},
            },
        ],
    }


def test_coordinate_round_trip_is_within_a_metre():
    lat, lng = 31.247979, 121.416775
    back = cc.gcj02_to_bd09(*cc.bd09_to_gcj02(lat, lng))
    assert haversine_m(lat, lng, *back) < 1.0


def test_gaps_follow_the_gray_regions_largest_first():
    gaps = cc.gaps_of(_caoyang())
    assert [(g.region, g.category, len(g.cells)) for g in gaps] == [
        ("A", "基础教育", 15),
        ("B", "基础教育", 2),
        ("C", "基础教育", 1),
    ]


def test_region_name_comes_from_the_reverse_geocoded_address():
    assert (
        cc.region_from_geocode({"address": "上海市普陀区曹杨路 1 号", "district": "普陀区"})
        == REGION
    )
    assert (
        cc.region_from_geocode({"address": "", "district": "西湖区", "city": "杭州市"})
        == "杭州市西湖区"
    )
    assert cc.region_from_geocode(None) is None


def test_suspects_are_only_unmatched_places_near_the_gap(tmp_path):
    feature = _caoyang()
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_answer(feature))

    async def go(transport):
        async with cc.AgentPlanClient("sk-ap-test", tmp_path, transport, pause_s=0) as ap:
            result = await cc.crosscheck(feature, ap, REGION)
            return result, cc.as_payload(result, REGION, ap)

    result, payload = asyncio.run(go(httpx.MockTransport(handler)))
    assert len(seen) == 3 and payload["agent_plan"] == {"requests": 3, "cache_hits": 0}
    first = seen[0]
    assert first.headers["Authorization"] == "Bearer sk-ap-test"
    assert first.url.params["region"] == REGION and first.url.params["sort"] == "distance"
    assert first.url.params["user_raw_request"] == cc.QUESTIONS["基础教育"]

    row = payload["rows"][0]
    names = {f["name"] for f in row["found"]}
    # 子点与托管班被挡掉；已收录的认出来；远处的在列表里但不算疑似
    assert names == {row["found"][0]["name"], "测试新开小学", "远处的小学"}
    assert row["matched"] == 1 and row["error"] is None
    suspect_names = {s["name"] for s in payload["suspects"]}
    assert suspect_names == {"测试新开小学"}
    assert all(s["nearest_gap_m"] <= cc.WALK_LIMIT_M for s in payload["suspects"])

    # 同样的问题不再问：第二次全部命中缓存
    def boom(request):
        raise AssertionError("不应该再发请求")

    _, again = asyncio.run(go(httpx.MockTransport(boom)))
    assert again["agent_plan"] == {"requests": 0, "cache_hits": 3}
    assert again["suspects"] == payload["suspects"]


def test_a_rejected_token_stops_and_never_reads_as_no_facility(tmp_path):
    feature = _caoyang()
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, json={"message": "invalid token"})

    async def go():
        async with cc.AgentPlanClient(
            "sk-ap-bad", tmp_path, httpx.MockTransport(handler), pause_s=0
        ) as ap:
            return cc.as_payload(await cc.crosscheck(feature, ap, REGION), REGION, ap)

    payload = asyncio.run(go())
    assert len(calls) == 1  # 第一个问题被拒，后面的不再问
    assert payload["aborted"] and "401" in payload["aborted"]
    assert all(r["error"] for r in payload["rows"]) and payload["suspects"] == []
    assert not list(tmp_path.glob("agent_plan/*.json"))  # 失败的回答不缓存，下次还能重问


def test_missing_token_is_fatal_before_any_request(tmp_path):
    async def go():
        async with cc.AgentPlanClient(None, tmp_path, pause_s=0) as ap:
            return await cc.crosscheck(_caoyang(), ap, REGION)

    result = asyncio.run(go())
    assert result.aborted and "BAIDU_MAP_AUTH_TOKEN" in result.aborted


def test_hourly_quota_caps_new_questions():
    quota = cc.HourlyQuota(limit=2)
    quota.take()
    quota.spend(2)
    with pytest.raises(cc.RateLimited):
        quota.take()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "analyses.db")
    with TestClient(main.app) as c:
        yield c


def _settings(tmp_path, token):
    return Settings(
        server_ak="test-server-ak", browser_ak="", cache_dir=tmp_path, agent_plan_token=token
    )


def test_endpoint_is_503_without_a_token(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "get_settings", lambda: _settings(tmp_path, ""))
    resp = client.post("/api/crosscheck", json={"feature": load_sample("isochrone-caoyang-15min")})
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "agent_plan_not_configured"


def test_endpoint_refuses_simulated_data(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "get_settings", lambda: _settings(tmp_path, "sk-ap-test"))
    feature = client.get("/api/demo", params={"lat": 31.25, "lng": 121.42}).json()
    resp = client.post("/api/crosscheck", json={"feature": feature})
    assert resp.status_code == 400 and resp.json()["detail"]["code"] == "simulated"


def test_endpoint_lists_suspects_and_never_echoes_the_token(client, tmp_path, monkeypatch):
    monkeypatch.setattr(main, "get_settings", lambda: _settings(tmp_path, "sk-ap-secret"))
    monkeypatch.setattr(main, "_require_server_ak", lambda: None)
    feature = load_sample("isochrone-caoyang-15min")

    async def fake_regeo(lat, lng):
        return {"address": "上海市普陀区曹杨路", "district": "普陀区"}

    monkeypatch.setattr(main.app.state.baidu, "reverse_geocode", fake_regeo)
    answer = _answer(_caoyang())
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=answer))
    real = cc.AgentPlanClient
    monkeypatch.setattr(
        cc, "AgentPlanClient", lambda token, cache_dir: real(token, cache_dir, transport, pause_s=0)
    )
    resp = client.post("/api/crosscheck", json={"feature": feature, "region": "A"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["region_name"] == REGION
    assert [r["region"] for r in body["rows"]] == ["A"]
    assert [s["name"] for s in body["suspects"]] == ["测试新开小学"]
    assert "sk-ap-secret" not in json.dumps(body, ensure_ascii=False)
