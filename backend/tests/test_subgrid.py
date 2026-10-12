"""50 米叠加细分：只细分边界格与临界格，子格用起点细键，占比按面积，父子不重复统计。"""

import asyncio
import copy

import pytest

from app.isochrone.geometry import haversine_m, point_in_polygon
from app.poi.catalog import by_name
from app.poi.collect import CategoryResult, CleanStats, CoverageResult, Poi
from app.report.blindspot import REACH_GRID_M, BlindspotConfig, identify_blindspots
from app.report.regions import gray_regions
from app.report.subgrid import recount, refined_view, total

CENTER = (31.248, 121.417)
SQUARE = [(31.243, 121.412), (31.253, 121.412), (31.253, 121.422), (31.243, 121.422)]
# 药店在中心正北 900 米：1 公里步行线从网格南部横穿，那一排格就是边界格
SHOP = Poi("药店", CENTER[0] + 900 / 111_320, CENTER[1], "医药")


class DistFake:
    """步行距离 = 直线距离：判定结果完全由几何决定，方便核对每个子格。"""

    def __init__(self):
        self.calls = []

    async def walking_matrix(self, origin, destinations):
        return [{"distance_m": 400.0, "duration_s": 300.0} for _ in destinations]

    async def walking_matrix_grid(
        self, origins, destinations, dest_grid_m=None, origin_grid_m=None
    ):
        if dest_grid_m == REACH_GRID_M and len(origins) == 1 and tuple(origins[0]) == CENTER:
            return [await self.walking_matrix(origins[0], destinations)]
        self.calls.append((list(origins), origin_grid_m, len(destinations)))
        return [
            [
                {"distance_m": haversine_m(*o, *d), "duration_s": haversine_m(*o, *d)}
                for d in destinations
            ]
            for o in origins
        ]


class CachedFake(DistFake):
    """带缓存的假客户端：missing_pairs 按缓存缺口计，matrix_pairs 是进程级累计值。"""

    def __init__(self, noise=0):
        super().__init__()
        self.seen = set()
        self.matrix_pairs = 0
        self.noise = noise  # 每次请求另加的点对：模拟同一进程里别的分析也在花

    @staticmethod
    def _key(o, d, dg, og):
        return (round(o[0], 6), round(o[1], 6), round(d[0], 6), round(d[1], 6), dg, og)

    def missing_pairs(self, mode, origins, destinations, dest_grid_m=None, origin_grid_m=None):
        return sum(
            self._key(o, d, dest_grid_m, origin_grid_m) not in self.seen
            for o in origins
            for d in destinations
        )

    async def walking_matrix_grid(
        self, origins, destinations, dest_grid_m=None, origin_grid_m=None
    ):
        for o in origins:
            for d in destinations:
                key = self._key(o, d, dest_grid_m, origin_grid_m)
                if key not in self.seen:
                    self.seen.add(key)
                    self.matrix_pairs += 1
        self.matrix_pairs += self.noise
        return await super().walking_matrix_grid(origins, destinations, dest_grid_m, origin_grid_m)


def _run(cfg=None, fake=None):
    coverage = CoverageResult(center=CENTER, radius_m=2500)
    coverage.results.append(CategoryResult(category="医药", pois=[SHOP], stats=CleanStats()))
    fake = fake or DistFake()
    result = asyncio.run(
        identify_blindspots(
            fake, CENTER, SQUARE, coverage, cfg or BlindspotConfig(), categories=(by_name("医药"),)
        )
    )
    return result, fake


def _far(lat, lng):
    return haversine_m(lat, lng, SHOP.lat, SHOP.lng) > 1000


def test_only_boundary_and_critical_cells_are_split():
    result, _ = _run()
    split = [c for c in result.cells if c.quarters]
    assert split and len(split) < len(result.cells)
    for c in split:
        near = c.nearest_m.get("医药")
        critical = near is not None and abs(near - 1000) <= 50
        assert critical or any(
            _far(c.lat, c.lng) != _far(o.lat, o.lng)
            for o in result.cells
            if o is not c and haversine_m(c.lat, c.lng, o.lat, o.lng) < 110
        )


def _outside(result):
    return [q for c in result.cells for q in c.quarters or [] if not q.in_circle]


def test_quarters_are_measured_with_fine_origin_keys_and_match_geometry():
    result, fake = _run()
    sub_calls = [c for c in fake.calls if c[1] == REACH_GRID_M]
    assert sub_calls  # 子格作起点按 10 米细键
    for c in result.cells:
        for q in c.quarters or []:
            if not q.in_circle:
                continue
            assert q.measured == ["医药"]
            assert ("医药" in q.missing) == _far(q.lat, q.lng)


def test_quarters_outside_the_circle_are_not_measured_or_counted():
    """父格心在圈内、子格中心在圈外：子格按自己的中心判圈内，圈外的不测、不判定、不计数。"""
    result, fake = _run()
    outside = _outside(result)
    assert outside  # 正方形南缘那一排格心正好压在边上，南边两个子格在圈外
    for q in outside:
        assert not point_in_polygon(q.lat, q.lng, SQUARE)
        assert q.measured == [] and q.missing == [] and q.unknown == []
    sent = {tuple(o) for origins, grid, _ in fake.calls if grid == REACH_GRID_M for o in origins}
    assert not sent & {(q.lat, q.lng) for q in outside}
    payload = result.as_dict()
    assert payload["refinement"]["outside_quarters"] == len(outside)
    for cell in payload["cells"]:
        for q in cell.get("quarters") or []:
            assert q["in_circle"] == point_in_polygon(q["lat"], q["lng"], SQUARE)
    # 派生的 50 米格网里也没有它们
    view = refined_view(payload)
    keys = {(round(c["lat"], 6), round(c["lng"], 6)) for c in view["cells"]}
    assert not keys & {(round(q.lat, 6), round(q.lng, 6)) for q in outside}


def test_ratio_is_area_weighted_without_double_counting():
    result, _ = _run()
    units = []
    for c in result.cells:
        units += [(q, 0.25) for q in c.quarters if q.in_circle] if c.quarters else [(c, 1.0)]
    area = sum(w for _, w in units)
    assert area == pytest.approx(len(result.cells) - 0.25 * len(_outside(result)))
    blind = sum(w for u, w in units if "医药" in u.missing)
    assert result.blind_ratio["医药"] == pytest.approx(blind / area)
    payload = result.as_dict()
    assert payload["blind_count"] == pytest.approx(blind)
    assert payload["refinement"]["by_category"]["医药"] == sum(
        1 for c in result.cells if c.quarters
    )


def test_refined_view_expands_every_cell_and_keeps_total_area():
    result, _ = _run()
    payload = result.as_dict()
    view = refined_view(payload)
    outside = len(_outside(result))
    assert view["grid_spacing_m"] == 50 and view["base_spacing_m"] == 100
    assert len(view["cells"]) == 4 * len(payload["cells"]) - outside
    assert total(view["cells"]) == pytest.approx(len(payload["cells"]) - 0.25 * outside)
    assert refined_view(view) is view  # 幂等
    # 没细分的格：4 个子格原样继承父格
    for i, cell in enumerate(payload["cells"]):
        kids = [c for c in view["cells"] if c["parent"] == i]
        if not cell.get("quarters"):
            assert all(k["missing"] == cell["missing"] and not k["split"] for k in kids)


def test_gray_regions_on_the_refined_grid_use_100m_cell_units():
    result, _ = _run()
    payload = result.as_dict()
    regions = gray_regions(payload, None)
    blind = sum(r["cells"] for r in regions["regions"])
    assert blind == pytest.approx(payload["blind_count"])
    assert sum(r["area_km2"] for r in regions["regions"]) == pytest.approx(blind * 0.01, abs=0.002)


def test_unrefined_results_are_unchanged():
    result, fake = _run(BlindspotConfig(refine_boundary=False))
    payload = result.as_dict()
    assert payload["refinement"] is None
    assert refined_view(payload) is payload
    assert not any(c[1] == REACH_GRID_M for c in fake.calls)


def test_recount_weights_quarters():
    result, _ = _run()
    view = refined_view(result.as_dict())
    cells = copy.deepcopy(view["cells"])
    for c in cells:
        c["missing"] = []
    after = recount(view, cells)
    assert after["blind_count"] == 0 and after["blind_ratio"]["医药"] == 0


def _sub_pairs(fake):
    return sum(len(origins) * n for origins, grid, n in fake.calls if grid == REACH_GRID_M)


def test_full_budget_refines_everything_and_reports_its_pairs():
    result, fake = _run()
    ref = result.as_dict()["refinement"]
    assert ref["stopped_categories"] == [] and ref["fallback_quarters"] == {}
    assert ref["pairs"] == ref["requested_pairs"] == _sub_pairs(fake) > 0
    assert ref["max_pairs"] == BlindspotConfig().refine_max_pairs


@pytest.mark.parametrize("share", [0.0, 0.1, 0.5])
def test_refine_budget_is_a_hard_cap(share):
    """预算是硬上限：每批请求前按缓存缺口算成本，超出剩余额度的那批不发，涉及的子格退回继承。"""
    full, full_fake = _run()
    need = _sub_pairs(full_fake)
    budget = int(need * share)
    result, fake = _run(BlindspotConfig(refine_max_pairs=budget))
    sent = _sub_pairs(fake)
    ref = result.as_dict()["refinement"]
    assert sent <= budget and ref["pairs"] == sent
    assert ref["stopped_categories"] == ["医药"] and ref["fallback_quarters"]["医药"] > 0
    full_q = {(q.lat, q.lng): q for c in full.cells for q in c.quarters or []}
    for c in result.cells:
        for q in c.quarters or []:
            if q.measured:
                # 测成的子格与不限预算时一样：没被拦的子格把它的候选都测完了
                assert q.missing == full_q[(q.lat, q.lng)].missing
            elif q.in_circle:
                assert q.missing == c.missing  # 被拦下的子格照旧继承父格
    if budget == 0:
        # 一个点对都不许花：只有直线就超过阈值、零请求即可判定的子格算实测
        assert sent == 0
        for c in result.cells:
            for q in c.quarters or []:
                if q.measured:
                    assert haversine_m(q.lat, q.lng, SHOP.lat, SHOP.lng) > 1030
                    assert q.missing == ["医药"]


def test_budget_of_one_pair_spends_at_most_one_pair():
    """评审复现的情形：预算 1 对，旧实现只在品类开始前看一眼，实际测了一整批。"""
    result, fake = _run(BlindspotConfig(refine_max_pairs=1))
    assert _sub_pairs(fake) <= 1
    assert result.as_dict()["refinement"]["pairs"] <= 1


def test_cached_pairs_do_not_count_against_the_budget():
    fake = CachedFake()
    first, _ = _run(fake=fake)
    again, _ = _run(BlindspotConfig(refine_max_pairs=0), fake=fake)
    a, b = first.as_dict(), again.as_dict()
    assert b["refinement"]["pairs"] == 0 and b["refinement"]["stopped_categories"] == []
    assert b["refinement"]["requested_pairs"] == a["refinement"]["requested_pairs"] > 0
    assert b["blind_count"] == a["blind_count"] and again.blind_ratio == first.blind_ratio


def test_budget_ignores_pairs_spent_elsewhere_in_the_process():
    """客户端是进程级单例：别的分析同时花的点对不算进这次细分的预算。"""
    result, _ = _run(fake=CachedFake(noise=10_000))
    ref = result.as_dict()["refinement"]
    assert ref["stopped_categories"] == [] and 0 < ref["pairs"] <= ref["max_pairs"]
