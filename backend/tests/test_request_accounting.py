"""请求前拦截不得计费，重试必须按每次尝试计数；传输全部使用 MockTransport。"""

import asyncio
from dataclasses import replace

import pytest

from app.baidu.client import request_observer
from app.baidu.errors import BaiduApiError


@pytest.mark.parametrize("grid", [True, False])
def test_budget_refused_matrix_does_not_count_as_sent(trip_provider, grid):
    def refuse(endpoint, params):
        raise BaiduApiError(-1, "test budget", endpoint)

    async def run():
        token = request_observer.set(refuse)
        try:
            if grid:
                await trip_provider.walking_matrix_grid([(31.25, 121.42)], [(31.26, 121.43)])
            else:
                await trip_provider.walking_matrix((31.25, 121.42), [(31.26, 121.43)])
        finally:
            request_observer.reset(token)

    asyncio.run(run())
    assert not trip_provider.test_hits
    assert trip_provider.matrix_pairs == 0
    assert not trip_provider.request_counts


def test_matrix_retries_are_counted_but_later_cache_hits_are_free(trip_provider):
    trip_provider._s = replace(trip_provider._s, max_retries=1, retry_backoff=0)
    trip_provider.test_answer = lambda endpoint, params: (
        {"status": 401, "message": "retry"} if len(trip_provider.test_hits) == 1 else None
    )

    async def run():
        for _ in range(2):
            values = await trip_provider.walking_matrix_grid(
                [(31.25, 121.42)], [(31.26, 121.43), (31.27, 121.44)]
            )
            assert all(values[0])

    asyncio.run(run())
    assert len(trip_provider.test_hits) == 2
    assert trip_provider.matrix_pairs == 4
    assert trip_provider.request_counts["matrix"] == 2
