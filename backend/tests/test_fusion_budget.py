"""新增预算是实际请求前的硬上限，缓存、并发与多起终点仍正确处理。"""

import asyncio
from contextlib import suppress

from app.baidu.client import request_observer
from app.baidu.errors import BaiduApiError
from app.isochrone.budget import FusionBudgetClient


def test_retry_attempts_share_the_route_request_cap():
    class RetryingNet(Net):
        async def walking_route(self, origin, destination, **kwargs):
            for _ in range(4):
                try:
                    request_observer.get()("/directionlite/v1/walking", {})
                except BaiduApiError:
                    return None
                self.actual_routes += 1
            return {"duration_s": 90}

    async def run():
        net = RetryingNet()
        client = FusionBudgetClient(net, 3, 2)
        assert await client.walking_route((1, 1), (2, 2)) is None
        assert net.actual_routes == client.route_attempts == 2
        assert client.refused_routes == 1
        assert request_observer.get() is None

    asyncio.run(run())


class Net:
    def __init__(self):
        self.seen = set()
        self.route_seen = set()
        self.actual_pairs = 0
        self.actual_routes = 0

    def missing_pairs(self, mode, origins, destinations, **kwargs):
        return sum((o, d) not in self.seen for o in origins for d in destinations)

    async def walking_matrix_grid(self, origins, destinations, **kwargs):
        self.actual_pairs += self.missing_pairs("walk", origins, destinations)
        self.seen.update((o, d) for o in origins for d in destinations)
        await asyncio.sleep(0)
        return [[{"distance_m": 100, "duration_s": 80} for _ in destinations] for _ in origins]

    def walking_route_cached(self, origin, destination, **kwargs):
        return (origin, destination) in self.route_seen

    async def walking_route(self, origin, destination, **kwargs):
        if not self.walking_route_cached(origin, destination):
            self.actual_routes += 1
        self.route_seen.add((origin, destination))
        return {"distance_m": 100, "duration_s": 90}


def test_partial_matrix_preserves_positions_and_never_overruns():
    async def run():
        net = Net()
        client = FusionBudgetClient(net, 3, 1)
        rows = await client.walking_matrix_grid([(1, 1), (2, 2)], [(3, 3), (4, 4)])
        assert rows[0][0] and rows[0][1] and rows[1][0]
        assert rows[1][1] is None
        assert net.actual_pairs == client.pairs_used == 3
        assert client.refused_pairs == 1
        # 预算用完后已经测过的点仍可从缓存读出。
        rows = await client.walking_matrix_grid([(1, 1)], [(3, 3), (4, 4)])
        assert all(rows[0])
        assert net.actual_pairs == 3

    asyncio.run(run())


def test_concurrent_consumers_share_the_same_remaining_budget():
    async def run():
        net = Net()
        client = FusionBudgetClient(net, 3, 0)
        await asyncio.gather(
            client.walking_matrix_grid([(1, 1)], [(2, 2), (3, 3)]),
            client.walking_matrix_grid([(4, 4)], [(5, 5), (6, 6)]),
        )
        assert net.actual_pairs == client.pairs_used == 3

    asyncio.run(run())


def test_route_cache_remains_available_when_new_route_budget_is_zero():
    async def run():
        net = Net()
        client = FusionBudgetClient(net, 0, 1)
        assert await client.walking_route((1, 1), (2, 2))
        assert await client.walking_route((1, 1), (2, 2))
        assert await client.walking_route((1, 1), (3, 3)) is None
        assert client.routes_used == net.actual_routes == 1

    asyncio.run(run())


def test_earlier_stages_cannot_borrow_reserved_route_or_matrix_budget():
    async def run():
        net = Net()
        total = FusionBudgetClient(net, 6, 4)
        total.pair_limit, total.route_limit = 3, 2
        rows = await total.walking_matrix_grid([(1, 1)], [(2, 2), (3, 3), (4, 4), (5, 5)])
        assert sum(v is not None for v in rows[0]) == 3
        for i in range(3):
            await total.walking_route((1, 1), (i + 2, 3))
        assert net.actual_routes == 2
        total.pair_limit, total.route_limit = 6, 4
        fusion = FusionBudgetClient(total, 3, 2)
        assert (await fusion.walking_matrix_grid([(1, 1)], [(5, 5)]))[0][0]
        assert await fusion.walking_route((1, 1), (7, 7))
        assert net.actual_pairs == 4 and net.actual_routes == 3

    asyncio.run(run())


def test_outer_budget_rejection_does_not_count_as_a_sent_inner_request():
    async def run():
        client = FusionBudgetClient(Net(), 10, 10)

        def deny(endpoint, params):
            raise BaiduApiError(-1, "whole analysis cap", endpoint)

        observer = client._observer(deny)
        for endpoint, params in [
            ("/directionlite/v1/walking", {}),
            ("/routematrix/v2/walking", {"origins": "1,1", "destinations": "2,2"}),
        ]:
            with suppress(BaiduApiError):
                observer(endpoint, params)
        assert client.route_attempts == client.pair_attempts == 0

    asyncio.run(run())
