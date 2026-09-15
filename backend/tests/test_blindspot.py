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
    _pick_candidates,
    identify_blindspots,
)

CENTER = (31.248, 121.417)
SQUARE = [
    (31.243, 121.412),
    (31.253, 121.412),
    (31.253, 121.422),
    (31.243, 121.422),
]


def _coverage(category: str, pois: list[Poi], failed: list[str] | None = None):
    coverage = CoverageResult(center=CENTER, radius_m=2400, failed=failed or [])
    if not failed:
        coverage.results.append(
            CategoryResult(category=category, pois=pois, stats=CleanStats())
        )
    return coverage


class FakeClient:
    """按预设距离表作答的算路桩。matrix 里 None 表示该点对测不出距离。"""

    def __init__(self, matrix: list[list[dict | None]], reach_s: float | None = 300.0):
        self.matrix = matrix
        self.reach_s = reach_s
        self.grid_calls = 0

    async def walking_matrix(self, origin, destinations):
        entry = None if self.reach_s is None else {"distance_m": 400.0, "duration_s": self.reach_s}
        return [entry for _ in destinations]

    async def walking_matrix_grid(self, origins, destinations):
        self.grid_calls += 1
        return [row[: len(destinations)] for row in self.matrix[: len(origins)]]


def test_pick_candidates_caps_to_max():
    cells = [CellResult(lat=31.248 + i * 0.001, lng=121.417) for i in range(10)]
    pois = [Poi(f"药店{i}", 31.240 + i * 0.002, 121.417, "医药") for i in range(20)]
    picked = _pick_candidates(cells, pois, BlindspotConfig(max_candidates=5))
    assert len(picked) == 5
    assert len({p.name for p in picked}) == 5


def test_failed_search_marks_unknown_not_blind():
    """接口失败绝不能伪装成盲区——本项目最危险的错误就是这一条。"""
    cells = [CellResult(lat=31.248, lng=121.417)]
    coverage = _coverage("医药", [], failed=["医药"])
    asyncio.run(
        _judge_category(FakeClient([[None]]), cells, by_name("医药"), coverage, BlindspotConfig())
    )
    assert cells[0].unknown == ["医药"]
    assert cells[0].missing == []
    assert not cells[0].is_blind


def test_all_candidates_unmeasurable_marks_unknown():
    cells = [CellResult(lat=31.248, lng=121.417)]
    coverage = _coverage("医药", [Poi("药店", 31.249, 121.418, "医药")])
    asyncio.run(
        _judge_category(
            FakeClient([[None, None]]), cells, by_name("医药"), coverage, BlindspotConfig()
        )
    )
    assert cells[0].unknown == ["医药"]
    assert cells[0].missing == []


def test_empty_category_after_successful_search_is_blind():
    """检索成功且一家都没有，才能判定盲区。"""
    cells = [CellResult(lat=31.248, lng=121.417)]
    coverage = _coverage("医药", [])
    asyncio.run(
        _judge_category(FakeClient([[None]]), cells, by_name("医药"), coverage, BlindspotConfig())
    )
    assert cells[0].missing == ["医药"]
    assert cells[0].is_blind


def test_nearest_beyond_walk_limit_is_blind():
    cells = [CellResult(lat=31.248, lng=121.417), CellResult(lat=31.249, lng=121.418)]
    coverage = _coverage("医药", [Poi("药店", 31.250, 121.419, "医药")])
    fake = FakeClient(
        [
            [{"distance_m": 1500.0, "duration_s": 1300.0}],  # 超 1 公里 -> 盲区
            [{"distance_m": 600.0, "duration_s": 500.0}],  # 1 公里内 -> 正常
        ]
    )
    asyncio.run(_judge_category(fake, cells, by_name("医药"), coverage, BlindspotConfig()))
    assert cells[0].missing == ["医药"]
    assert cells[1].missing == []
    assert cells[1].nearest_m["医药"] == 600.0


def test_identify_blindspots_reports_ratio_and_heat():
    coverage = _coverage("医药", [Poi("药店", 31.250, 121.419, "医药")])
    fake = FakeClient([[{"distance_m": 1500.0, "duration_s": 1300.0}]] * 200)
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
    cells = [CellResult(lat=31.248, lng=121.417), CellResult(lat=31.249, lng=121.418)]
    coverage = _coverage("医药", [Poi("药店", 31.250, 121.419, "医药")])
    fake = FakeClient(
        [
            [{"distance_m": 1500.0, "duration_s": 1300.0}],  # 盲区
            [None],  # 未知
        ]
    )
    asyncio.run(_judge_category(fake, cells, by_name("医药"), coverage, BlindspotConfig()))
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
                {
                    "distance_m": None,
                    "distance": {"value": (o_base + i) * 100 + j},
                    "duration": {"value": 60},
                }
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

    assert costs[1] == pytest.approx(costs[0] * 25)  # 4 点对 -> 100 点对


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
