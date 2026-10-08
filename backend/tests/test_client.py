"""百度客户端：配额与配置错误的上报、缓存有效期、步行路线解析。"""

import asyncio
import os
import time

import pytest

from app.baidu.client import BaiduMapClient
from app.baidu.errors import BaiduApiError, ConfigurationError, QuotaExhaustedError
from app.config import Settings


def _client(tmp_path, **kw) -> BaiduMapClient:
    return BaiduMapClient(
        Settings(server_ak=kw.pop("ak", "test-ak"), browser_ak="", cache_dir=tmp_path, **kw)
    )


def test_route_matrix_surfaces_quota_exhaustion(tmp_path, monkeypatch):
    """配额耗尽不能被填成 None：那会让等时圈把整批点当成障碍、画出面积为 0 的圈。"""
    client = _client(tmp_path)

    async def fake_request(endpoint, params, cost=1.0):
        raise QuotaExhaustedError(302, "天配额超限", endpoint)

    monkeypatch.setattr(client, "_request", fake_request)
    with pytest.raises(QuotaExhaustedError):
        asyncio.run(client.route_matrix("walk", (31.24, 121.41), [(31.25, 121.42)]))


def test_route_matrix_records_ordinary_failures(tmp_path, monkeypatch):
    client = _client(tmp_path)

    async def fake_request(endpoint, params, cost=1.0):
        raise BaiduApiError(401, "并发超限", endpoint)

    monkeypatch.setattr(client, "_request", fake_request)
    out = asyncio.run(client.route_matrix("walk", (31.24, 121.41), [(31.25, 121.42)]))
    assert out == [None]
    assert len(client.matrix_failures) == 1


def test_expired_cache_is_refetched(tmp_path, monkeypatch):
    client = _client(tmp_path, cache_ttl_s=60.0)
    hits = []

    async def fake_request(endpoint, params, cost=1.0):
        hits.append(1)
        return {"result": [{"distance": {"value": 500}, "duration": {"value": 427}}]}

    monkeypatch.setattr(client, "_request", fake_request)
    origins, dests = [(31.24, 121.41)], [(31.25, 121.42)]
    asyncio.run(client.walking_matrix_grid(origins, dests))
    asyncio.run(client.walking_matrix_grid(origins, dests))
    assert len(hits) == 1  # 有效期内命中缓存

    old = time.time() - 3600
    for root, _, files in os.walk(tmp_path):
        for name in files:
            os.utime(os.path.join(root, name), (old, old))
    asyncio.run(client.walking_matrix_grid(origins, dests))
    assert len(hits) == 2  # 过期后回源


def test_poi_search_raises_configuration_errors(tmp_path, monkeypatch):
    """AK 或白名单错误要原样上报，不能伪装成「查询失败」。"""
    client = _client(tmp_path)

    async def fake_request(endpoint, params, cost=1.0):
        raise ConfigurationError(210, "IP 校验失败", endpoint)

    monkeypatch.setattr(client, "_request", fake_request)
    with pytest.raises(ConfigurationError):
        asyncio.run(client.search_poi("药店", 31.24, 121.41))


def test_poi_search_transient_failure_is_none(tmp_path, monkeypatch):
    client = _client(tmp_path)

    async def fake_request(endpoint, params, cost=1.0):
        raise QuotaExhaustedError(302, "天配额超限", endpoint)

    monkeypatch.setattr(client, "_request", fake_request)
    assert asyncio.run(client.search_poi("药店", 31.24, 121.41)) is None


def test_walking_route_parses_steps_and_caches(tmp_path, monkeypatch):
    client = _client(tmp_path)
    calls = []

    async def fake_request(endpoint, params, cost=1.0):
        calls.append(endpoint)
        return {
            "status": 0,
            "result": {
                "routes": [
                    {
                        "distance": 64,
                        "duration": 96,
                        "steps": [
                            {
                                "distance": 39,
                                "duration": 63,
                                "turn_type": "过马路左转",
                                "instruction": "走40米,<b>过马路左转</b>进入<b>曹杨路</b>",
                                "path": "121.41677,31.24834;121.41674,31.24862",
                            },
                            {
                                "distance": 25,
                                "duration": 33,
                                "turn_type": "无效",
                                "instruction": "走30米,到达<b>终点</b>",
                                "path": "121.41674,31.24862;bad;121.41650,31.24880",
                            },
                        ],
                    }
                ]
            },
        }

    monkeypatch.setattr(client, "_request", fake_request)
    route = asyncio.run(client.walking_route((31.24834, 121.41677), (31.2488, 121.4165)))
    assert route is not None
    assert route["distance_m"] == 64.0
    assert route["steps"][0]["instruction"] == "走40米,过马路左转进入曹杨路"
    assert route["steps"][0]["path"][0] == [31.24834, 121.41677]
    assert len(route["steps"][1]["path"]) == 2  # 坏点被跳过
    again = asyncio.run(client.walking_route((31.24834, 121.41677), (31.2488, 121.4165)))
    assert again == route
    assert calls == ["/directionlite/v1/walking"]


def test_walking_route_failure_is_recorded_not_raised(tmp_path, monkeypatch):
    client = _client(tmp_path)

    async def fake_request(endpoint, params, cost=1.0):
        raise ConfigurationError(240, "服务被禁用", endpoint)

    monkeypatch.setattr(client, "_request", fake_request)
    assert asyncio.run(client.walking_route((31.24, 121.41), (31.25, 121.42))) is None
    assert len(client.route_failures) == 1


def test_client_starts_without_ak_but_refuses_requests(tmp_path):
    """没配 AK 时后端要能启动（样例、离线模拟照常可用），真正发请求时再报配置错误。"""
    client = _client(tmp_path, ak="")

    async def run():
        async with client:
            await client.geocode("上海市普陀区")

    with pytest.raises(ConfigurationError):
        asyncio.run(run())


# ---------- 配额熔断按自然日解除 ----------


def test_daily_quota_breaker_resets_after_beijing_midnight(tmp_path, monkeypatch):
    """302 是当日配额：常驻进程过了零点必须自动恢复，否则后端不重启就一直拒绝。"""
    import app.baidu.client as client_mod

    client = _client(tmp_path)
    monkeypatch.setattr(client_mod, "_beijing_day", lambda: "2026-09-24")
    client._mark_exhausted("/place/v2/search", 302)
    client._mark_exhausted("/place/v2/search", 302)  # 同一天只记一次
    assert len(client.quota_events) == 1
    assert client.quota_events[0]["service"] == "地点检索"
    with pytest.raises(QuotaExhaustedError):
        client._check_exhausted("/place/v2/search")

    monkeypatch.setattr(client_mod, "_beijing_day", lambda: "2026-09-25")
    client._check_exhausted("/place/v2/search")  # 不再抛出


def test_public_quota_state_and_events_renew_without_an_intermediate_request(tmp_path, monkeypatch):
    import app.baidu.client as client_mod

    client = _client(tmp_path)
    endpoint = "/directionlite/v1/walking"
    assert client.exhausted(endpoint) is None
    monkeypatch.setattr(client_mod, "_beijing_day", lambda: "2026-10-07")
    client._mark_exhausted(endpoint, 302)
    assert client.exhausted(endpoint).status == 302
    assert client.exhausted("/place/v2/search") is None
    with pytest.raises(QuotaExhaustedError):
        client._check_exhausted(endpoint)
    assert len(client.quota_events) == 1

    monkeypatch.setattr(client_mod, "_beijing_day", lambda: "2026-10-08")
    # 直接再次标记也应清掉昨日状态，而不是漏掉今天的新事件。
    client._mark_exhausted(endpoint, 302)
    assert client.exhausted(endpoint).status == 302
    assert [e["day"] for e in client.quota_events] == ["2026-10-07", "2026-10-08"]
    monkeypatch.setattr(client_mod, "_beijing_day", lambda: "2026-10-09")
    assert client.exhausted(endpoint) is None


def test_permanent_quota_breaker_stays(tmp_path, monkeypatch):
    import app.baidu.client as client_mod

    client = _client(tmp_path)
    monkeypatch.setattr(client_mod, "_beijing_day", lambda: "2026-09-24")
    client._mark_exhausted("/routematrix/v2/walking", 301)
    monkeypatch.setattr(client_mod, "_beijing_day", lambda: "2026-09-25")
    assert client.exhausted("/routematrix/v2/walking").status == 301
    with pytest.raises(QuotaExhaustedError) as info:
        client._check_exhausted("/routematrix/v2/walking")
    assert info.value.status == 301


# ---------- 实时路况：新鲜期 + 过期兜底 ----------


def _age_all(root, seconds):
    old = time.time() - seconds
    for base, _, files in os.walk(root):
        for name in files:
            os.utime(os.path.join(base, name), (old, old))


def test_traffic_cache_is_fresh_for_ttl_then_refetched(tmp_path, monkeypatch):
    client = _client(tmp_path, traffic_ttl_s=600.0)
    hits = []

    async def fake_request(endpoint, params, cost=1.0):
        hits.append(1)
        return {"result": [{"distance": {"value": 3000}, "duration": {"value": 420}}]}

    monkeypatch.setattr(client, "_request", fake_request)
    origin, dest = (31.24, 121.41), [(31.26, 121.43)]
    asyncio.run(client.route_matrix("drive_traffic", origin, dest))
    asyncio.run(client.route_matrix("drive_traffic", origin, dest))
    assert len(hits) == 1  # 10 分钟内视为实时
    _age_all(tmp_path, 900)
    asyncio.run(client.route_matrix("drive_traffic", origin, dest))
    assert len(hits) == 2  # 过期回源
    assert client.traffic_stale == []


def test_traffic_falls_back_to_stale_cache_when_api_fails(tmp_path, monkeypatch):
    """路况「部分保留」：过期条目不删，接口失败时兜底，并记下它有多旧。"""
    client = _client(tmp_path, traffic_ttl_s=600.0)

    async def ok_request(endpoint, params, cost=1.0):
        return {"result": [{"distance": {"value": 3000}, "duration": {"value": 420}}]}

    monkeypatch.setattr(client, "_request", ok_request)
    origin, dest = (31.24, 121.41), [(31.26, 121.43)]
    asyncio.run(client.route_matrix("drive_traffic", origin, dest))
    _age_all(tmp_path, 1800)

    async def quota_request(endpoint, params, cost=1.0):
        raise QuotaExhaustedError(302, "天配额超限", endpoint)

    monkeypatch.setattr(client, "_request", quota_request)
    out = asyncio.run(client.route_matrix("drive_traffic", origin, dest))
    assert out == [{"distance_m": 3000.0, "duration_s": 420.0}]
    assert len(client.traffic_stale) == 1 and client.traffic_stale[0] >= 1700
    assert "QuotaExhaustedError" in (client.traffic_stale_reason or "")


def test_traffic_without_any_old_data_still_raises(tmp_path, monkeypatch):
    client = _client(tmp_path)

    async def quota_request(endpoint, params, cost=1.0):
        raise QuotaExhaustedError(302, "天配额超限", endpoint)

    monkeypatch.setattr(client, "_request", quota_request)
    with pytest.raises(QuotaExhaustedError):
        asyncio.run(client.route_matrix("drive_traffic", (31.24, 121.41), [(31.26, 121.43)]))


def test_free_flow_driving_is_not_treated_as_traffic(tmp_path, monkeypatch):
    """畅通路况（tactics=13）不随时间变化，沿用 30 天缓存，不做过期兜底。"""
    client = _client(tmp_path, traffic_ttl_s=600.0)
    hits = []

    async def fake_request(endpoint, params, cost=1.0):
        hits.append(1)
        return {"result": [{"distance": {"value": 3000}, "duration": {"value": 300}}]}

    monkeypatch.setattr(client, "_request", fake_request)
    origin, dest = (31.24, 121.41), [(31.26, 121.43)]
    asyncio.run(client.route_matrix("drive", origin, dest))
    _age_all(tmp_path, 900)
    asyncio.run(client.route_matrix("drive", origin, dest))
    assert len(hits) == 1
    assert client.traffic_ages == []


# ---------- 并发 ----------


def test_matrix_requests_run_concurrently_but_within_the_cap(tmp_path, monkeypatch):
    """在途请求数要真的并行起来，又不能突破上限：上限之外的请求排队等名额。"""
    client = _client(tmp_path, matrix_concurrency=3, max_qps=1000)
    inflight = {"now": 0, "peak": 0}

    async def slow_request(endpoint, params, cost=1.0):
        inflight["now"] += 1
        inflight["peak"] = max(inflight["peak"], inflight["now"])
        await asyncio.sleep(0.05)
        inflight["now"] -= 1
        n = len(params["origins"].split("|")) * len(params["destinations"].split("|"))
        return {"result": [{"distance": {"value": 500}, "duration": {"value": 427}}] * n}

    monkeypatch.setattr(client, "_request", slow_request)

    async def run():
        await asyncio.gather(
            *(
                client.walking_matrix_grid([(31.24 + i * 0.01, 121.41)], [(31.30, 121.50)])
                for i in range(9)
            )
        )

    started = time.perf_counter()
    asyncio.run(run())
    elapsed = time.perf_counter() - started
    assert inflight["peak"] == 3
    assert client.matrix_inflight_peak == 3
    assert client.matrix_inflight == 0
    # 9 个请求、3 路并发：约 3 轮 × 0.05 秒，远小于串行的 0.45 秒
    assert elapsed < 0.3


def test_concurrency_one_restores_serial(tmp_path, monkeypatch):
    client = _client(tmp_path, matrix_concurrency=1, max_qps=1000)

    async def slow_request(endpoint, params, cost=1.0):
        await asyncio.sleep(0.01)
        return {"result": [{"distance": {"value": 500}, "duration": {"value": 427}}]}

    monkeypatch.setattr(client, "_request", slow_request)

    async def run():
        await asyncio.gather(
            *(
                client.walking_matrix_grid([(31.24 + i * 0.01, 121.41)], [(31.30, 121.50)])
                for i in range(4)
            )
        )

    asyncio.run(run())
    assert client.matrix_inflight_peak == 1


def test_backoff_has_jitter(tmp_path):
    client = _client(tmp_path)
    waits = {round(client._backoff(1), 4) for _ in range(20)}
    assert len(waits) > 1  # 同时撞上 401 的请求不会在同一时刻醒来
    assert all(0.5 * 0.5 * 2 <= w <= 1.5 * 0.5 * 2 for w in waits)


# ---------- 多起点矩阵只请求缺的点对 ----------


def _echo_matrix(calls):
    """按请求里的起终点个数如实作答，并记下每次请求的点对数。"""

    async def fake_request(endpoint, params, cost=1.0):
        n_o = len(params["origins"].split("|"))
        n_d = len(params["destinations"].split("|"))
        calls.append(n_o * n_d)
        return {
            "result": [
                {"distance": {"value": 500}, "duration": {"value": 400}} for _ in range(n_o * n_d)
            ]
        }

    return fake_request


def test_matrix_grid_requests_only_uncached_origins(tmp_path, monkeypatch):
    """盲区判定是「多起点 × 1 终点」：测过的起点不该跟着没测过的一起重发。"""
    client = _client(tmp_path)
    calls: list[int] = []
    monkeypatch.setattr(client, "_request", _echo_matrix(calls))
    dest = [(31.30, 121.50)]
    origins = [(31.24 + i * 0.001, 121.41) for i in range(4)]

    asyncio.run(client.walking_matrix_grid(origins[:2], dest))
    table = asyncio.run(client.walking_matrix_grid(origins, dest))

    assert calls == [2, 2]  # 第二次只发了没测过的两个起点
    assert client.matrix_pairs == 4
    assert all(row[0] is not None for row in table)


def test_matrix_grid_groups_origins_by_their_missing_destinations(tmp_path, monkeypatch):
    client = _client(tmp_path)
    calls: list[int] = []
    monkeypatch.setattr(client, "_request", _echo_matrix(calls))
    o = [(31.24, 121.41), (31.25, 121.41)]
    d = [(31.30, 121.50), (31.31, 121.50)]

    asyncio.run(client.walking_matrix_grid([o[0]], [d[0]]))  # 先把 (o0, d0) 测进缓存
    calls.clear()
    table = asyncio.run(client.walking_matrix_grid(o, d))

    # o0 只缺 d1，o1 两个都缺：分两块发，共 3 个点对，而不是整块 4 个
    assert sorted(calls) == [1, 2]
    assert client.matrix_pairs == 1 + 3
    assert all(cell is not None for row in table for cell in row)


def test_matrix_grid_raises_quota_but_keeps_what_other_blocks_measured(tmp_path, monkeypatch):
    """配额耗尽要向上抛（调用方据此改用估算）；同时已经测到的块照样写进缓存。"""
    client = _client(tmp_path, matrix_batch_size=2)
    state = {"n": 0}

    async def fake_request(endpoint, params, cost=1.0):
        state["n"] += 1
        if state["n"] == 2:
            raise QuotaExhaustedError(302, "天配额超限", endpoint)
        n_o = len(params["origins"].split("|"))
        return {"result": [{"distance": {"value": 500}, "duration": {"value": 400}}] * n_o}

    monkeypatch.setattr(client, "_request", fake_request)
    dest = [(31.30, 121.50)]
    origins = [(31.24 + i * 0.001, 121.41) for i in range(4)]
    with pytest.raises(QuotaExhaustedError):
        asyncio.run(client.walking_matrix_grid(origins, dest))

    calls: list[int] = []
    monkeypatch.setattr(client, "_request", _echo_matrix(calls))
    asyncio.run(client.walking_matrix_grid(origins, dest))
    assert calls == [2]  # 第一块已在缓存里，只补第二块


def test_quota_breaker_sends_nothing_and_counts_no_pairs(tmp_path, monkeypatch):
    client = _client(tmp_path)
    sent: list[int] = []
    monkeypatch.setattr(client, "_request", _echo_matrix(sent))
    client._mark_exhausted("/routematrix/v2/walking", 302)

    with pytest.raises(QuotaExhaustedError):
        asyncio.run(client.walking_matrix_grid([(31.24, 121.41)], [(31.30, 121.50)]))
    with pytest.raises(QuotaExhaustedError):
        asyncio.run(client.route_matrix("walk", (31.24, 121.41), [(31.30, 121.50)]))
    assert sent == [] and client.matrix_pairs == 0


def test_poi_scope_two_is_requested_and_cached_separately(tmp_path, monkeypatch):
    """scope=2 带导航点与子点：要真的传给百度，而且不能命中 scope=1 的旧缓存。"""
    client = _client(tmp_path)
    seen: list[dict] = []

    async def fake_request(endpoint, params, cost=1.0):
        seen.append(dict(params))
        detail = {"detail_info": {"navi_location": {"lat": 31.2, "lng": 121.4}}}
        record = {"name": "某小学", **(detail if "scope" in params else {})}
        return {"status": 0, "results": [record]}

    monkeypatch.setattr(client, "_request", fake_request)
    plain = asyncio.run(client.search_poi("小学", 31.24, 121.41))
    rich = asyncio.run(client.search_poi("小学", 31.24, 121.41, scope=2))
    again = asyncio.run(client.search_poi("小学", 31.24, 121.41, scope=2))
    assert "scope" not in seen[0] and seen[1]["scope"] == 2
    assert len(seen) == 2  # 第三次命中 scope=2 的缓存
    assert "detail_info" not in plain[0]
    assert (
        rich[0]["detail_info"]["navi_location"]["lat"]
        == 31.2
        == again[0]["detail_info"]["navi_location"]["lat"]
    )
