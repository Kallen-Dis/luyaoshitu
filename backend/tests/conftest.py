"""出行测试的零联网百度传输与独立缓存、预算目录。"""

import httpx
import pytest

from app.baidu.client import BaiduMapClient
from app.config import Settings
from app.demo import build_demo_feature
from app.isochrone.geometry import haversine_m


@pytest.fixture
def trip_feature():
    feature = build_demo_feature(31.25, 121.42, 15, 36)
    feature["properties"]["simulated"] = False
    feature["properties"]["coverage"]["places"] = []
    return feature


@pytest.fixture
def trip_provider(tmp_path):
    client = BaiduMapClient(
        Settings(
            server_ak="test-ak",
            browser_ak="",
            cache_dir=tmp_path / "cache",
            markings_dir=tmp_path / "budget",
            max_qps=100000,
            max_retries=0,
        )
    )
    client.test_hits = []
    client.test_answer = None

    def transport(request):
        params = dict(request.url.params)
        endpoint = request.url.path
        client.test_hits.append((endpoint, {k: v for k, v in params.items() if k != "ak"}))
        if client.test_answer:
            answer = client.test_answer(endpoint, params)
            if answer is not None:
                return httpx.Response(200, json=answer)
        if "/place/v2/search" in endpoint:
            return httpx.Response(200, json={"status": 0, "results": [], "total": 0})
        if "routematrix" in endpoint:
            origins = [tuple(map(float, p.split(","))) for p in params["origins"].split("|")]
            destinations = [
                tuple(map(float, p.split(","))) for p in params["destinations"].split("|")
            ]
            results = []
            for a in origins:
                for b in destinations:
                    value = haversine_m(*a, *b) * 1.25
                    results.append(
                        {"distance": {"value": value}, "duration": {"value": value / 1.17}}
                    )
            return httpx.Response(200, json={"status": 0, "result": results})
        a = tuple(map(float, params["origin"].split(",")))
        b = tuple(map(float, params["destination"].split(",")))
        value = haversine_m(*a, *b) * 1.3
        path = f"{a[1]},{a[0]};{b[1]},{b[0]}"
        return httpx.Response(
            200,
            json={
                "status": 0,
                "result": {
                    "routes": [
                        {
                            "distance": value,
                            "duration": value / 1.17 + 30,
                            "steps": [
                                {
                                    "path": path,
                                    "instruction": "向前步行，过马路",
                                    "distance": value,
                                    "duration": value / 1.17 + 30,
                                }
                            ],
                        }
                    ]
                },
            },
        )

    client._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    yield client
    # MockTransport 没有真实连接；close 不依赖驻留事件循环。
    import asyncio

    asyncio.run(client._client.aclose())
