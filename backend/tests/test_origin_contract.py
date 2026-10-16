"""请求起点和分段折线起点分别核验；不制造起步连线、耗时或位置证据。"""

import asyncio
from copy import deepcopy

import pytest
from test_gridcheck import POLY, C, FakeNet, _rays

from app.isochrone.fusion import estimate_fusion
from app.isochrone.geometry import haversine_m, offset_point
from app.isochrone.gridcheck import GridcheckConfig, _Checker
from app.isochrone.routequality import check_origin, path_prefix_usable, same_origin


class ContractNet(FakeNet):
    def __init__(self, gap=25, mismatched=False, missing=False):
        super().__init__(pocket=False)
        self.gap, self.mismatched, self.missing = gap, mismatched, missing

    async def walking_route(self, origin, destination, **kwargs):
        data = await super().walking_route(origin, destination, **kwargs)
        # 各方向的首个折线点不同，但记录的请求/响应origin相同。
        bearing = 90 if destination[1] > origin[1] else 270
        start = offset_point(*origin, bearing, self.gap)
        data["steps"][0]["path"][0] = start
        data["steps"][0]["start_location"] = {"lat": start[0], "lng": start[1]}
        response = offset_point(*origin, 0, 20) if self.mismatched else origin
        data["response_origin"] = {"lat": response[0], "lng": response[1]}
        if not self.missing:
            data["requested_origin"] = list(origin)
        return data


def checker(net):
    chk = _Checker(net, C, POLY, None, GridcheckConfig(fusion_mode=True, max_routes=8))
    points = [
        chk.add((0, i), 0, "cell", *offset_point(*C, a, 200))
        for i, a in enumerate((45, 135, 225, 315))
    ]
    return chk, points


def test_response_contract_accepts_different_step_starts_without_moving_origin_or_adding_path():
    chk, points = checker(ContractNet())
    result = asyncio.run(check_origin(chk, points))
    assert result["passed"] and chk.center == C and result["origin_groups"] == 1
    assert all(p.route_origin == C and same_origin(p, C) for p in points)
    assert all(haversine_m(*p.route_start, *C) > 20 for p in points)
    paths = [deepcopy(p.route_path) for p in points]
    fusion = estimate_fusion(chk, _rays(chk.client))
    assert fusion["quality"]["origin_conflicts"] == 0
    assert fusion["origin_anchor"] == list(C)
    assert all(p.route_path == path for p, path in zip(points, paths, strict=True))
    assert all(path[0] != C for path in paths)
    assert len(fusion["training_paths"]) == 4


@pytest.mark.parametrize("kwargs", [{"mismatched": True}, {"missing": True}])
def test_echo_alone_wrong_origin_or_large_missing_start_never_passes(kwargs):
    chk, points = checker(ContractNet(**kwargs))
    assert not asyncio.run(check_origin(chk, points))["passed"]
    assert all(not same_origin(p, C) for p in points)


def test_cached_request_from_nearby_center_cannot_be_reused_as_same_origin():
    chk, points = checker(ContractNet())
    asyncio.run(check_origin(chk, points))
    assert not same_origin(points[0], offset_point(*C, 90, 5))


@pytest.mark.parametrize("gap", [49.9, 50.3, 90])
def test_prefix_gap_does_not_erase_valid_request_and_endpoint_evidence(gap):
    chk, points = checker(ContractNet(gap=gap))
    assert asyncio.run(check_origin(chk, points))["passed"]
    assert all(same_origin(p, C) and p.route_authoritative for p in points)
    fusion = estimate_fusion(chk, _rays(chk.client))
    assert len(fusion["training_paths"]) == (4 if gap <= 52 else 0)
    assert all(path_prefix_usable(p, C) == (gap <= 52) for p in points)


def test_one_bad_direction_does_not_block_three_valid_directions():
    class Mixed(ContractNet):
        async def walking_route(self, origin, destination, **kwargs):
            data = await super().walking_route(origin, destination, **kwargs)
            if destination[0] > origin[0] and destination[1] > origin[1]:
                data["response_origin"] = {"lat": origin[0] + 0.01, "lng": origin[1]}
            return data

    chk, _ = checker(Mixed())
    points = [
        chk.add((5, i), 0, "cell", *offset_point(*C, a, 200))
        for i, a in enumerate((45, 135, 225, 315))
    ]
    result = asyncio.run(check_origin(chk, points))
    assert result["passed"] and sum(r["usable"] for r in result["records"]) == 3
