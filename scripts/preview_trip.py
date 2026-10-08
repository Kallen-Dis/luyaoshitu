"""本地浏览器验收服务器：算路全部用假响应，零百度配额；仅绑定 127.0.0.1。

启动：python scripts/preview_trip.py；前端 npm run dev:preview，访问 5174。
此脚本不用于生产；所有出行结果都会带「验收假接口」说明。
"""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import httpx
import uvicorn
from fastapi.responses import JSONResponse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
os.environ["BAIDU_SERVER_AK"] = "trip-preview-test"
os.environ["MARKINGS_DIR"] = str(ROOT / ".cache" / "trip-preview-user")
os.environ["TRIP_DAY_PAIRS"] = "100000"
os.environ["TRIP_HOUR_PAIRS"] = "100000"

from app import main, storage  # noqa: E402
from app.baidu.cache import DiskCache  # noqa: E402
from app.isochrone.geometry import haversine_m  # noqa: E402

storage.DB_PATH = ROOT / ".cache" / "trip-preview-history.sqlite3"


def transport(request: httpx.Request) -> httpx.Response:
    p = dict(request.url.params)
    endpoint = request.url.path
    if "routematrix" in endpoint:
        origins = [tuple(map(float, t.split(","))) for t in p["origins"].split("|")]
        destinations = [tuple(map(float, t.split(","))) for t in p["destinations"].split("|")]
        result = []
        for a in origins:
            for b in destinations:
                distance = haversine_m(*a, *b) * 1.3
                result.append(
                    {
                        "distance": {"value": distance},
                        "duration": {"value": distance / 1.17},
                    }
                )
        return httpx.Response(200, json={"status": 0, "result": result})
    if "directionlite" in endpoint:
        a = tuple(map(float, p["origin"].split(",")))
        b = tuple(map(float, p["destination"].split(",")))
        distance = haversine_m(*a, *b) * 1.32
        path = f"{a[1]},{a[0]};{b[1]},{a[0]};{b[1]},{b[0]}"
        return httpx.Response(
            200,
            json={
                "status": 0,
                "result": {
                    "routes": [
                        {
                            "distance": distance,
                            "duration": distance / 1.17 + 30,
                            "steps": [
                                {
                                    "path": path,
                                    "instruction": "本地验收示例：向前步行，过马路。",
                                    "distance": distance,
                                    "duration": distance / 1.17 + 30,
                                }
                            ],
                        }
                    ]
                },
            },
        )
    return httpx.Response(200, json={"status": 0, "results": [], "total": 0})


@asynccontextmanager
async def lifespan(app):
    async with main.lifespan(app):
        await app.state.baidu._client.aclose()
        preview_cache = ROOT / ".cache" / "trip-preview-cache"
        app.state.baidu._s = replace(app.state.baidu._s, cache_dir=preview_cache)
        app.state.baidu._cache = DiskCache(preview_cache, app.state.baidu._s.cache_grid_m)
        app.state.baidu._client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        yield


main.app.router.lifespan_context = lifespan


@main.app.middleware("http")
async def label_preview(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/trip/") and response.status_code == 200:
        import json

        body = b"".join([chunk async for chunk in response.body_iterator])
        result = json.loads(body)
        result["preview"] = True
        result["warnings"].insert(0, "本地验收假接口，不代表百度真实步行路网（零真实算路请求）。")
        return JSONResponse(result)
    return response


if __name__ == "__main__":
    uvicorn.run(main.app, host="127.0.0.1", port=8001, log_level="warning")
