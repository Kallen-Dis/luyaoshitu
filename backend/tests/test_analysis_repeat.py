"""计算结果可直接重算；展示精度不能改变中心点和缓存键。"""

import asyncio

from app.main import AnalysisState, IsochroneRequest, _analysis_steps
from app.travel import WALK


def test_repeat_from_returned_center_reuses_every_ray_and_route(trip_provider, monkeypatch):
    async def no_markings(*args, **kwargs):
        return []

    monkeypatch.setattr("app.main.markings_for_analysis", no_markings)

    async def calculate(center):
        req = IsochroneRequest(lat=center[0], lng=center[1], coverage=False)
        feature = None
        async for _, data in _analysis_steps(
            trip_provider, req, WALK, center, AnalysisState(), None
        ):
            if "feature" in data:
                feature = data["feature"]
        return feature

    async def check():
        # 在真实浏览器验收中，round(..., 6) 会使这处中心的两个射线终点丢缓存。
        center = (31.245832551131617, 121.44073754811537)
        first = await calculate(center)
        before = len(trip_provider.test_hits)
        saved = first["properties"]["center"]
        second = await calculate((saved["lat"], saved["lng"]))
        assert len(trip_provider.test_hits) == before
        assert first["geometry"] == second["geometry"]

    asyncio.run(check())
