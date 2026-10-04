"""学校按校门测距：剪枝、测距、成因诊断、检索与缓存都按入口，拿不到入口时退回坐标点。"""

import asyncio

from app.baidu.client import BaiduMapClient
from app.config import Settings
from app.poi.catalog import by_name
from app.poi.collect import CategoryResult, CleanStats, CoverageResult, Poi, clean
from app.poi.entries import ENTRY_GRID_M, lookup_gates, straight_m
from app.report.blindspot import BlindspotConfig, CellResult, _judge_category, rank_candidates
from app.report.regions import classify_missing

M = 1 / 111_320.0  # 一米对应的纬度
CELL = (31.25, 121.42)
SCHOOL = "基础教育"


def north(m: float) -> tuple[float, float]:
    return round(CELL[0] + m * M, 6), CELL[1]


def school(point_m: float, gates_m: tuple[float, ...] = (), name: str = "某小学") -> Poi:
    """坐标点在网格正北 point_m 米，校门在正北 gates_m 米处。"""
    lat, lng = north(point_m)
    entries = tuple((*north(g), f"{i + 1}号门") for i, g in enumerate(gates_m))
    return Poi(name, lat, lng, SCHOOL, entries=entries, raw_name=name)


class GateClient:
    """步行距离 = 到终点的直线 × 1.2；在 `broken` 里的终点测不出来。记录每次的终点与细键参数。"""

    def __init__(self, broken: set[tuple[float, float]] | None = None) -> None:
        self.broken = broken or set()
        self.calls: list[tuple[list, float | None]] = []

    async def walking_matrix_grid(self, origins, destinations, dest_grid_m=None):
        self.calls.append((list(destinations), dest_grid_m))
        rows = []
        for o in origins:
            row = []
            for d in destinations:
                if (round(d[0], 6), round(d[1], 6)) in self.broken:
                    row.append(None)
                else:
                    dist = abs(d[0] - o[0]) / M * 1.2
                    row.append({"distance_m": dist, "duration_s": dist})
            rows.append(row)
        return rows


def _judge(client, pois):
    cells = [CellResult(lat=CELL[0], lng=CELL[1])]
    coverage = CoverageResult(center=CELL, radius_m=2500)
    coverage.results.append(CategoryResult(category=SCHOOL, pois=pois, stats=CleanStats()))
    asyncio.run(_judge_category(client, cells, by_name(SCHOOL), coverage, BlindspotConfig()))
    return cells[0]


def test_gate_close_enough_even_though_the_point_is_far():
    """坐标点在校园深处（步行 1080 米），校门在路边（步行 780 米）：够得着。"""
    cell = _judge(GateClient(), [school(900, (650,))])
    assert cell.missing == []
    assert round(cell.nearest_m[SCHOOL]) == 780


def test_point_close_but_the_only_gate_is_far():
    """坐标点吸附到没有门的围墙外：按点测会误判够得着，按门测是缺。"""
    cell = _judge(GateClient(), [school(700, (950,))])
    assert cell.missing == [SCHOOL]


def test_entries_use_the_fine_cache_key_and_points_do_not():
    client = GateClient()
    _judge(client, [school(700, (650, 690))])
    dests, fine = client.calls[0]
    assert fine == ENTRY_GRID_M and len(dests) == 2
    client = GateClient()
    _judge(client, [school(700)])
    assert client.calls[0][1] is None and len(client.calls[0][0]) == 1


def test_one_gate_unmeasured_and_the_rest_too_far_is_unknown():
    """一个门没测到、测到的门都走不到：不能断言这所学校走不到。"""
    far = school(700, (950, 800))
    client = GateClient(broken={north(800)})
    cell = _judge(client, [far])
    assert cell.unknown == [SCHOOL] and cell.missing == []


def test_pruning_uses_the_nearest_gate():
    cell = CellResult(lat=CELL[0], lng=CELL[1])
    pois = [school(1100, (950,), "门近点远"), school(1050, (), "只有坐标点")]
    assert rank_candidates(cell, pois, 1000.0) == [0]
    # 直线法对照仍按坐标点：两所都超过 1 公里
    assert rank_candidates(cell, pois, 1000.0, entrances=False) == []


def test_cause_diagnosis_uses_entries():
    lat, lng = north(1100)
    gate_lat, _ = north(950)
    place = {
        "category": SCHOOL,
        "name": "门近点远",
        "lat": lat,
        "lng": lng,
        "entries": [{"lat": gate_lat, "lng": lng, "name": "南门"}],
    }
    cells = [{"lat": CELL[0], "lng": CELL[1], "missing": [SCHOOL]}]
    _, causes = classify_missing(cells, {"places": [place], "categories": {SCHOOL: 0}}, 1000.0)
    assert causes[0][SCHOOL]["cause"] == "barrier"  # 直线 950 米就有门，步行却走不到
    assert round(straight_m(CELL[0], CELL[1], place)) == 950


def test_clean_reads_the_navigation_point_and_drops_adult_education_by_tag():
    records = [
        {
            "name": "某某小学(东校区)",
            "location": {"lat": 31.251, "lng": 121.42},
            "detail_info": {
                "tag": "教育培训;小学",
                "navi_location": {"lat": 31.2512, "lng": 121.42},
            },
        },
        {
            "name": "某某进修学院",
            "location": {"lat": 31.252, "lng": 121.42},
            "detail_info": {"tag": "教育培训;成人教育"},
        },
    ]
    pois, stats = clean(records, by_name(SCHOOL), CELL, 2500)
    assert [p.name for p in pois] == ["某某小学"]
    assert pois[0].raw_name == "某某小学(东校区)"
    assert pois[0].entries == ((31.2512, 121.42, "导航点"),)
    assert stats.excluded == 1


class SearchClient:
    def __init__(self, pages):
        self.pages = pages
        self.asked: list[tuple[str, int]] = []

    async def search_poi(self, keyword, lat, lng, radius, page_size=20, page_num=0, scope=1):
        self.asked.append((keyword, scope))
        return self.pages.get(keyword)


def test_gate_lookup_adds_gates_and_keeps_the_navigation_point_on_failure():
    with_gates = school(500, (480,), "有门的小学")
    failed = school(600, (590,), "查不到的小学")
    page = [
        {
            "name": "有门的小学",
            "location": {"lat": with_gates.lat, "lng": with_gates.lng},
            "detail_info": {
                "children": [
                    {
                        "name": "有门的小学-北门",
                        "classified_poi_tag": "出入口;门",
                        "location": {"lat": north(520)[0], "lng": CELL[1]},
                    },
                    {
                        "name": "有门的小学-地上停车场",
                        "classified_poi_tag": "交通设施;停车场",
                        "location": {"lat": north(530)[0], "lng": CELL[1]},
                    },
                ]
            },
        }
    ]
    client = SearchClient({"有门的小学": page, "查不到的小学": None})
    out, n = asyncio.run(lookup_gates(client, [with_gates, failed], CELL))
    assert n == 2 and all(scope == 2 for _, scope in client.asked)
    assert [e[2] for e in out[0].entries] == ["北门"]
    assert out[1].entries == failed.entries  # 检索失败：保留原来的入口，不当成查询失败


def test_client_keeps_entry_pairs_apart_from_the_coarse_cache(tmp_path, monkeypatch):
    settings = Settings(server_ak="test-ak", browser_ak="", cache_dir=tmp_path, max_qps=1000)
    client = BaiduMapClient(settings)
    sent = []

    async def fake_request(endpoint, params, cost=1.0):
        dests = params["destinations"].split("|")
        sent.append(len(dests))
        result = [
            {"distance": {"value": 100 + i}, "duration": {"value": 80}} for i in range(len(dests))
        ]
        return {"status": 0, "result": result}

    monkeypatch.setattr(client, "_request", fake_request)
    o = [CELL]
    # 两个门相距 25 米：50 米的粗键会把它们并成一个，细键必须分开
    gates = [north(500), north(525)]
    first = asyncio.run(client.walking_matrix_grid(o, gates, dest_grid_m=ENTRY_GRID_M))
    again = asyncio.run(client.walking_matrix_grid(o, gates, dest_grid_m=ENTRY_GRID_M))
    coarse = asyncio.run(client.walking_matrix_grid(o, [gates[0]]))
    # 首次两个门一起测；第二次全部命中细键缓存；粗键没有被细键结果冒充，另测一次
    assert sent == [2, 1]
    assert again == first
    assert first[0][0] != first[0][1]
    assert coarse[0][0] is not None
