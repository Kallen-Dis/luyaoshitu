import asyncio

import pytest

from app.baidu.client import BaiduMapClient
from app.config import Settings
from app.poi.catalog import by_name
from app.poi.collect import CategoryResult, CleanStats, CoverageResult, Poi
from app.report.blindspot import (
    BlindspotConfig,
    CellResult,
    _judge_category,
    identify_blindspots,
    rank_candidates,
)

CENTER = (31.248, 121.417)
SQUARE = [
    (31.243, 121.412),
    (31.253, 121.412),
    (31.253, 121.422),
    (31.243, 121.422),
]

METERS_PER_DEG_LAT = 111_320.0


def _coverage(category: str, pois: list[Poi], failed: list[str] | None = None):
    coverage = CoverageResult(center=CENTER, radius_m=2400, failed=failed or [])
    if not failed:
        coverage.results.append(CategoryResult(category=category, pois=pois, stats=CleanStats()))
    return coverage


def poi_at(name: str, straight_m: float, lng: float = 121.417) -> Poi:
    """在 CENTER 正北 straight_m 米处放一个设施，便于精确控制直线距离。"""
    return Poi(name, CENTER[0] + straight_m / METERS_PER_DEG_LAT, lng, "医药")


class FakeClient:
    """按「设施名 -> 步行距离」作答的算路桩。

    值为 None 表示该点对测不出距离（不可达或请求失败）。同时统计请求数与点对数——
    盲区判定的配额消耗就是由这两个数决定的，故它们本身就是要断言的对象。
    """

    def __init__(self, distances: dict[str, float | None], reach_s: float | None = 300.0):
        self.distances = distances
        self.reach_s = reach_s
        self.requests = 0
        self.pairs = 0
        self._by_coord: dict[tuple[float, float], float | None] = {}

    def register(self, pois: list[Poi]) -> "FakeClient":
        for p in pois:
            self._by_coord[(round(p.lat, 6), round(p.lng, 6))] = self.distances.get(p.name)
        return self

    async def walking_matrix(self, origin, destinations):
        entry = None if self.reach_s is None else {"distance_m": 400.0, "duration_s": self.reach_s}
        return [entry for _ in destinations]

    async def walking_matrix_grid(self, origins, destinations):
        self.requests += 1
        self.pairs += len(origins) * len(destinations)
        rows = []
        for _ in origins:
            row = []
            for d in destinations:
                dist = self._by_coord.get((round(d[0], 6), round(d[1], 6)))
                row.append(None if dist is None else {"distance_m": dist, "duration_s": dist})
            rows.append(row)
        return rows


def _judge(fake: FakeClient, cells, pois, cfg=None):
    fake.register(pois)
    asyncio.run(
        _judge_category(
            fake, cells, by_name("医药"), _coverage("医药", pois), cfg or BlindspotConfig()
        )
    )


def test_rank_candidates_drops_those_beyond_straight_limit():
    """直线距离是步行距离的下界，超标的设施连请求都不该发。"""
    cell = CellResult(lat=CENTER[0], lng=CENTER[1])
    pois = [poi_at("远", 1500), poi_at("近", 300), poi_at("中", 800)]
    ranked = rank_candidates(cell, pois, 1000.0)
    assert [pois[i].name for i in ranked] == ["近", "中"]


def test_failed_search_marks_unknown_not_blind():
    """接口失败绝不能伪装成盲区——本项目最危险的错误就是这一条。"""
    cells = [CellResult(lat=CENTER[0], lng=CENTER[1])]
    fake = FakeClient({})
    asyncio.run(
        _judge_category(
            fake,
            cells,
            by_name("医药"),
            _coverage("医药", [], failed=["医药"]),
            BlindspotConfig(),
        )
    )
    assert cells[0].unknown == ["医药"]
    assert cells[0].missing == []
    assert not cells[0].is_blind
    assert fake.requests == 0


def test_empty_category_after_successful_search_is_blind():
    """检索成功且一家都没有，才能判定盲区。"""
    cells = [CellResult(lat=CENTER[0], lng=CENTER[1])]
    fake = FakeClient({})
    _judge(fake, cells, [])
    assert cells[0].missing == ["医药"]
    assert fake.requests == 0


def test_straight_line_beyond_limit_is_blind_without_any_request():
    """桃浦镇的情形：最近的设施直线就超 1 公里，零消耗即可严格判定。"""
    cells = [CellResult(lat=CENTER[0], lng=CENTER[1])]
    fake = FakeClient({"远处菜场": 1200.0})
    _judge(fake, cells, [poi_at("远处菜场", 1091)])
    assert cells[0].missing == ["医药"]
    assert fake.requests == 0
    assert fake.pairs == 0


def test_nearest_candidate_within_limit_resolves_in_one_round():
    cells = [CellResult(lat=CENTER[0], lng=CENTER[1])]
    fake = FakeClient({"近店": 600.0, "次近店": 900.0})
    _judge(fake, cells, [poi_at("近店", 300), poi_at("次近店", 700)])
    assert cells[0].missing == []
    assert cells[0].unknown == []
    assert cells[0].nearest_m["医药"] == 600.0
    assert fake.pairs == 1  # 第一家就达标，第二家不必测


def test_falls_through_to_next_candidate_when_nearest_is_cut_off():
    """直线最近的那家被河道隔开走不到，由更远的候选接管，不算盲区。"""
    cells = [CellResult(lat=CENTER[0], lng=CENTER[1])]
    fake = FakeClient({"隔河店": 1800.0, "绕得到的店": 850.0})
    _judge(fake, cells, [poi_at("隔河店", 300), poi_at("绕得到的店", 700)])
    assert cells[0].missing == []
    assert cells[0].nearest_m["医药"] == 850.0
    assert fake.pairs == 2


def test_all_candidates_exhausted_is_strictly_blind():
    cells = [CellResult(lat=CENTER[0], lng=CENTER[1])]
    fake = FakeClient({"甲": 1500.0, "乙": 1600.0})
    _judge(fake, cells, [poi_at("甲", 300), poi_at("乙", 700)])
    assert cells[0].missing == ["医药"]
    assert fake.pairs == 2


def test_measurement_failure_marks_unknown_not_blind():
    cells = [CellResult(lat=CENTER[0], lng=CENTER[1])]
    fake = FakeClient({"测不出的店": None})
    _judge(fake, cells, [poi_at("测不出的店", 300)])
    assert cells[0].unknown == ["医药"]
    assert cells[0].missing == []


def test_cells_sharing_nearest_candidate_are_packed_into_one_request():
    """同组共用一个终点，矩阵退化成一列：点对数等于网格数，这是最省配额的形状。"""
    cells = [CellResult(lat=CENTER[0] + i * 0.0001, lng=CENTER[1]) for i in range(8)]
    fake = FakeClient({"共同最近店": 500.0})
    _judge(fake, cells, [poi_at("共同最近店", 300)])
    assert fake.requests == 1
    assert fake.pairs == 8
    assert all(not c.is_blind for c in cells)


def test_identify_blindspots_reports_ratio_and_heat():
    pois = [poi_at("够不着的店", 900)]
    fake = FakeClient({"够不着的店": 1500.0})
    coverage = _coverage("医药", pois)
    fake.register(pois)
    result = asyncio.run(
        identify_blindspots(
            fake,
            CENTER,
            SQUARE,
            coverage,
            BlindspotConfig(grid_spacing_m=200.0),
            categories=(by_name("医药"),),
        )
    )
    assert result.cells
    assert all(c.reach_s == 300.0 for c in result.cells)
    assert result.blind_ratio["医药"] == pytest.approx(1.0)
    assert len(result.blind_cells) == len(result.cells)

    payload = result.as_dict()
    assert payload["blind_count"] == payload["cell_count"]
    assert payload["walk_limit_m"] == 1000.0
    assert payload["max_reach_s"] == 300


def test_blind_ratio_ignores_unknown_cells():
    """未知网格不参与占比，否则失败的点会被算成"不是盲区"而拉低盲区率。"""
    # 两个网格相距约 3 公里，各自只有一家设施在直线阈值内，判定互不干扰
    far_lng = CENTER[1] + 0.03
    cells = [
        CellResult(lat=CENTER[0], lng=CENTER[1]),
        CellResult(lat=CENTER[0], lng=far_lng),
    ]
    pois = [poi_at("超标店", 200), poi_at("测不出的店", 200, lng=far_lng)]
    fake = FakeClient({"超标店": 1500.0, "测不出的店": None})
    _judge(fake, cells, pois)

    assert cells[0].missing == ["医药"]  # 唯一候选走不到，严格盲区
    assert cells[1].unknown == ["医药"]  # 测不出，不能下结论
    judged = [c for c in cells if "医药" not in c.unknown]
    assert len(judged) == 1
    assert sum(1 for c in judged if "医药" in c.missing) == 1


def _client(tmp_path, batch: int) -> BaiduMapClient:
    return BaiduMapClient(
        Settings(
            server_ak="test-ak",
            browser_ak="",
            matrix_batch_size=batch,
            cache_dir=tmp_path,
        )
    )


def test_matrix_grid_flattens_origin_major(tmp_path, monkeypatch):
    """百度把 M×N 的结果压成一维数组按起点优先展开，索引算错会让整张表错位。"""
    origins = [(31.240 + i * 0.001, 121.410) for i in range(3)]
    dests = [(31.250, 121.420 + j * 0.001) for j in range(2)]
    client = _client(tmp_path, batch=4)
    calls: list[int] = []

    async def fake_request(endpoint, params, cost=1.0):
        n_o = len(params["origins"].split("|"))
        n_d = len(params["destinations"].split("|"))
        calls.append(n_o * n_d)
        o_base = int(float(params["origins"].split("|")[0].split(",")[0]) * 1000) - 31240
        return {
            "result": [
                {"distance": {"value": (o_base + i) * 100 + j}, "duration": {"value": 60}}
                for i in range(n_o)
                for j in range(n_d)
            ]
        }

    monkeypatch.setattr(client, "_request", fake_request)
    table = asyncio.run(client.walking_matrix_grid(origins, dests))

    assert [[c["distance_m"] for c in row] for row in table] == [
        [0.0, 1.0],
        [100.0, 101.0],
        [200.0, 201.0],
    ]
    # 预算 4 个点对：前两个起点各带 2 个终点凑满一批，第三个起点单独一批
    assert calls == [4, 2]


def test_matrix_grid_serves_repeat_call_from_cache(tmp_path, monkeypatch):
    origins = [(31.240, 121.410), (31.241, 121.410)]
    dests = [(31.250, 121.420)]
    client = _client(tmp_path, batch=100)
    hits = []

    async def fake_request(endpoint, params, cost=1.0):
        hits.append(1)
        n = len(params["origins"].split("|")) * len(params["destinations"].split("|"))
        return {
            "result": [{"distance": {"value": 500}, "duration": {"value": 400}} for _ in range(n)]
        }

    monkeypatch.setattr(client, "_request", fake_request)
    first = asyncio.run(client.walking_matrix_grid(origins, dests))
    second = asyncio.run(client.walking_matrix_grid(origins, dests))

    assert first == second
    assert len(hits) == 1  # 第二次整块命中缓存，零消耗


def test_matrix_request_cost_scales_with_point_pairs(tmp_path, monkeypatch):
    """大矩阵要占用更多令牌：按请求数计的限速会让 100 点对的请求与单点查询同价。"""
    client = _client(tmp_path, batch=100)
    costs: list[float] = []

    async def fake_request(endpoint, params, cost=1.0):
        costs.append(cost)
        n = len(params["origins"].split("|")) * len(params["destinations"].split("|"))
        return {
            "result": [{"distance": {"value": 500}, "duration": {"value": 400}} for _ in range(n)]
        }

    monkeypatch.setattr(client, "_request", fake_request)
    small = [(31.240 + i * 0.001, 121.410) for i in range(2)]
    dests = [(31.250, 121.420 + j * 0.001) for j in range(2)]
    asyncio.run(client.walking_matrix_grid(small, dests))

    big = [(31.260 + i * 0.001, 121.410) for i in range(10)]
    big_dests = [(31.270, 121.420 + j * 0.001) for j in range(10)]
    asyncio.run(client.walking_matrix_grid(big, big_dests))

    assert len(costs) == 3  # 4 点对 + 100 点对按官方步行上限切成两个 50 对请求
    assert costs[1] == costs[2] == pytest.approx(costs[0] * 12.5)


def test_failed_matrix_block_is_counted(tmp_path, monkeypatch):
    """整块失败必须留痕：它在结果里与真实的不可达完全同形。"""
    from app.baidu.errors import BaiduApiError

    client = _client(tmp_path, batch=100)

    async def fake_request(endpoint, params, cost=1.0):
        raise BaiduApiError(401, "并发超限", endpoint)

    monkeypatch.setattr(client, "_request", fake_request)
    table = asyncio.run(client.walking_matrix_grid([(31.24, 121.41)], [(31.25, 121.42)]))

    assert table == [[None]]
    assert client.failed_matrix_blocks == 1


# ---------- 施工围挡与过街等待 ----------

from app.isochrone.geometry import grid_points, offset_point  # noqa: E402
from app.isochrone.refine import Closure, DelayProfile, RefineResult  # noqa: E402
from app.report.blindspot import (  # noqa: E402
    ClosureCheck,
    available_pois,
    route_may_touch,
)


def test_ellipse_test_skips_closures_far_from_the_route():
    a = CENTER
    b = offset_point(*CENTER, 0, 600)
    on_way = Closure(*offset_point(*CENTER, 0, 300), 30.0)
    far_side = Closure(*offset_point(*CENTER, 90, 800), 30.0)
    assert route_may_touch(a, b, 700.0, on_way)
    assert not route_may_touch(a, b, 700.0, far_side)


def test_facility_inside_closure_is_unavailable():
    pois = [poi_at("围挡里的店", 300), poi_at("正常店", 700)]
    closure = Closure(pois[0].lat, pois[0].lng, 40.0)
    kept, excluded = available_pois(pois, (closure,))
    assert [p.name for p in kept] == ["正常店"]
    assert excluded == 1


class RouteFake(FakeClient):
    """在 FakeClient 基础上按终点给出路线折线，用于围挡核验。"""

    def __init__(self, distances, paths):
        super().__init__(distances)
        self.paths = paths  # 终点坐标 -> 折线
        self.route_calls = 0

    async def walking_route(self, origin, destination):
        self.route_calls += 1
        path = self.paths.get((round(destination[0], 6), round(destination[1], 6)))
        if path is None:
            return None
        return {"distance_m": 0, "duration_s": 0, "steps": [{"path": path}]}


def test_route_through_closure_falls_through_to_next_candidate():
    """最近那家的最短路线穿过围挡，换下一家绕得开的，不算盲区。"""
    near = poi_at("近店", 300)
    far = poi_at("远店", 700)
    closure = Closure(*offset_point(*CENTER, 0, 150), 30.0)
    detour_mid = offset_point(*CENTER, 90, 200)
    fake = RouteFake(
        {"近店": 400.0, "远店": 900.0},
        {
            (round(near.lat, 6), round(near.lng, 6)): [list(CENTER), [near.lat, near.lng]],
            (round(far.lat, 6), round(far.lng, 6)): [
                list(CENTER),
                list(detour_mid),
                [far.lat, far.lng],
            ],
        },
    )
    fake.register([near, far])
    cells = [CellResult(lat=CENTER[0], lng=CENTER[1])]
    check = ClosureCheck(budget=10)
    asyncio.run(
        _judge_category(
            fake,
            cells,
            by_name("医药"),
            _coverage("医药", [near, far]),
            BlindspotConfig(),
            (closure,),
            check,
        )
    )
    assert cells[0].missing == []
    assert cells[0].nearest_m["医药"] == 900.0
    assert check.checked == 2 and check.blocked == 1


def test_unverifiable_closure_route_is_unknown_not_blind():
    near = poi_at("近店", 300)
    closure = Closure(*offset_point(*CENTER, 0, 150), 30.0)
    fake = RouteFake({"近店": 400.0}, {})  # 路线取不到
    fake.register([near])
    cells = [CellResult(lat=CENTER[0], lng=CENTER[1])]
    check = ClosureCheck(budget=10)
    asyncio.run(
        _judge_category(
            fake,
            cells,
            by_name("医药"),
            _coverage("医药", [near]),
            BlindspotConfig(),
            (closure,),
            check,
        )
    )
    assert cells[0].unknown == ["医药"]
    assert cells[0].missing == []
    assert check.unverified == 1


def test_heat_adds_crossing_delay_and_grid_stays_inside_the_circle():
    pois = [poi_at("近店", 100)]
    fake = FakeClient({"近店": 300.0}, reach_s=300.0)
    fake.register(pois)
    profile = DelayProfile(knots_s=[0.0, 1000.0], knots_delay=[0.0, 100.0], length_m=1000.0)
    refine = RefineResult(
        bearings=[0.0, 90.0, 180.0, 270.0],
        profiles=[profile] * 4,
        base_speed=1.17,
        delay_applied=True,
        closures=(),
    )
    result = asyncio.run(
        identify_blindspots(
            fake,
            CENTER,
            SQUARE,
            _coverage("医药", pois),
            BlindspotConfig(),
            categories=(by_name("医药"),),
            refine=refine,
        )
    )
    # 现版只在 15 分钟圈内按 100 米布点：灰色区域只标圈内
    assert len(result.cells) == len(grid_points(SQUARE, 100.0)) > 50
    assert all(c.in_circle for c in result.cells)
    # 批量算路给 400 米 / 300 秒，路线剖面在 400 米处累计 40 秒
    assert all(c.reach_raw_s == 300.0 for c in result.cells)
    assert all(c.reach_s == pytest.approx(340.0) for c in result.cells)
    payload = result.as_dict()
    assert payload["basis"] == "network"
    assert payload["layout"] == "polygon" and payload["extent_m"] is None
    assert payload["delay_applied"] is True


def test_legacy_disc_results_are_scoped_to_the_circle():
    """较早的结果铺满中心 1.5 公里：打开时只留圈内格子，占比与计数按圈内重算。"""
    from app.report.blindspot import scope_to_circle

    def cell(inside, missing, unknown=()):
        return {
            "lat": 31.2,
            "lng": 121.4,
            "in_circle": inside,
            "missing": list(missing),
            "unknown": list(unknown),
            "reach_s": 600 if inside else 1800,
        }

    legacy = {
        "layout": "disc",
        "extent_m": 1500,
        "grid_spacing_m": 200,
        "cell_count": 5,
        "blind_count": 4,
        "blind_ratio": {"医药": 0.8, "基础教育": 0.2},
        "incomplete_categories": [],
        "cells": [
            cell(True, ["医药"]),
            cell(True, []),
            cell(False, ["医药"]),
            cell(False, ["医药", "基础教育"]),
            cell(False, ["医药"]),
        ],
    }
    scoped = scope_to_circle(legacy)
    assert scoped["layout"] == "polygon" and scoped["scoped_from"] == "disc"
    assert scoped["cell_count"] == 2 and scoped["blind_count"] == 1
    assert scoped["blind_ratio"] == {"医药": 0.5, "基础教育": 0.0}
    assert scoped["max_reach_s"] == 600
    assert legacy["cell_count"] == 5  # 不改动原对象
    assert scope_to_circle(scoped) is scoped  # 现版结果原样返回


def test_groups_within_a_round_are_measured_concurrently():
    """同一轮里共用不同候选的各组互不相干，应并发发出；结论与串行完全一致。"""

    class SlowFake(FakeClient):
        def __init__(self, distances):
            super().__init__(distances)
            self.now = 0
            self.peak = 0

        async def walking_matrix_grid(self, origins, destinations):
            self.now += 1
            self.peak = max(self.peak, self.now)
            await asyncio.sleep(0.02)
            self.now -= 1
            return await super().walking_matrix_grid(origins, destinations)

    # 三个网格各自最近的候选不同 → 第一轮三组
    cells = [CellResult(lat=CENTER[0], lng=CENTER[1] + i * 0.02) for i in range(3)]
    pois = [poi_at(f"店{i}", 200, lng=CENTER[1] + i * 0.02) for i in range(3)]
    fake = SlowFake({"店0": 500.0, "店1": 1500.0, "店2": 600.0})
    _judge(fake, cells, pois)
    assert fake.peak == 3
    assert [c.missing for c in cells] == [[], ["医药"], []]


def test_quota_exhausted_mid_judging_marks_unknown_not_blind():
    """配额在判定途中用完：没测到的格子记「未知」，不能算成盲区，也不能让整个分析失败。"""
    from app.baidu.errors import QuotaExhaustedError

    class QuotaClient(FakeClient):
        async def walking_matrix_grid(self, origins, destinations):
            raise QuotaExhaustedError(302, "天配额超限", "/routematrix/v2/walking")

    cells = [CellResult(lat=CENTER[0], lng=CENTER[1])]
    _judge(QuotaClient({}), cells, [poi_at("近", 300)])
    assert cells[0].unknown == ["医药"]
    assert cells[0].missing == []
