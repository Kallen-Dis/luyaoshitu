"""工地 POI 候选：名称筛选、去重、按是否压在步行路线上排序，检索失败如实上报。"""

import asyncio

from app.isochrone.geometry import offset_point
from app.poi.construction import KEYWORDS, classify_name, find_construction

CENTER = (31.25, 121.42)


def test_name_filter_keeps_sites_and_drops_shops():
    assert classify_name("中建八局桃浦项目部") == "strong"
    assert classify_name("某某地块工地") == "strong"
    assert classify_name("道路施工") == "weak"
    assert classify_name("上海某某建筑施工有限公司") is None
    assert classify_name("工地劳务招聘") is None
    assert classify_name("施工建材店") is None
    assert classify_name("") is None


def _poi(name, north_m, east_m=0.0):
    lat, lng = offset_point(*offset_point(*CENTER, 0, north_m), 90, east_m)
    return {"name": name, "location": {"lat": lat, "lng": lng}, "address": "某路"}


class FakePoiClient:
    def __init__(self, pages):
        self.pages = pages
        self.queries = 0

    def usage_snapshot(self):
        return {"requests": {"poi": self.queries}}

    async def search_poi(self, keyword, lat, lng, radius, page_size=20, page_num=0):
        self.queries += 1
        return self.pages.get(keyword)


def test_candidates_on_a_walking_route_come_first_and_duplicates_merge():
    near_route = _poi("北侧工地", 600, 20)
    far_off = _poi("东侧工地", 0, 400)
    client = FakePoiClient(
        {
            "工地": [far_off, near_route, _poi("工地建材市场", 300)],
            # 同一处工地被两个关键词都检索到，只留一条
            "施工": [_poi("北侧工地", 600, 25), _poi("施工劳务", 100)],
        }
    )
    route = [CENTER, offset_point(*CENTER, 0, 1200)]
    feature = {
        "properties": {"rays": [{"bearing": 0.0, "route_path": [[lng, lat] for lat, lng in route]}]}
    }
    out = asyncio.run(find_construction(client, CENTER, 1500, feature))
    names = [c["name"] for c in out["candidates"]]
    assert names == ["北侧工地", "东侧工地"]
    first = out["candidates"][0]
    assert first["on_route"] is True and first["on_route_bearing"] == 0.0
    assert out["candidates"][1]["on_route"] is False
    assert out["dropped"] == 2 and out["raw_count"] == 5
    assert out["poi_queries"] == len(KEYWORDS)


def test_failed_keywords_are_reported_not_treated_as_empty():
    out = asyncio.run(find_construction(FakePoiClient({"工地": []}), CENTER, 1500))
    assert out["failed_keywords"] == ["施工"]
    assert out["candidates"] == []
