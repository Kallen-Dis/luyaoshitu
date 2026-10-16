"""一次融合升级共享的请求前预算：缓存不计，不用进程级计数差推算本次花费。"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..baidu.client import request_observer
from ..baidu.errors import BaiduApiError


class FusionBudgetClient:
    def __init__(self, client: Any, max_pairs: int, max_routes: int) -> None:
        self.client = client
        self.max_pairs = max(0, max_pairs)
        self.max_routes = max(0, max_routes)
        self.pairs_used = 0
        self.routes_used = 0
        self.refused_pairs = 0
        self.refused_routes = 0
        self.pair_attempts = 0
        self.route_attempts = 0
        self.route_limit = self.max_routes
        self.pair_limit = self.max_pairs
        self.refused_points: set[tuple] = set()

    def pair_was_refused(self, origin, destination, dest_grid_m=None, origin_grid_m=None):
        return (origin, destination, dest_grid_m, origin_grid_m) in self.refused_points

    def _observer(self, previous):
        def observe(endpoint, params):
            if "directionlite" in endpoint and "walking" in endpoint:
                if self.route_attempts >= self.route_limit:
                    self.refused_routes += 1
                    raise BaiduApiError(-1, "融合路线请求预算已用完（含重试）", endpoint)
            elif "routematrix" in endpoint:
                pairs = len(params["origins"].split("|")) * len(params["destinations"].split("|"))
                if self.pair_attempts + pairs > self.pair_limit:
                    self.refused_pairs += pairs
                    raise BaiduApiError(-1, "融合点对请求预算已用完（含重试）", endpoint)
            if previous:
                previous(endpoint, params)
            # 上层总预算也接受后才计入本层，拒绝的请求没有发出。
            if "directionlite" in endpoint and "walking" in endpoint:
                self.route_attempts += 1
            elif "routematrix" in endpoint:
                self.pair_attempts += pairs

        return observe

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)

    def _cost(self, origin, destination, dest_grid_m, origin_grid_m) -> int:
        probe = getattr(self.client, "missing_pairs", None)
        if probe is None:
            return 1
        return int(
            probe(
                "walk",
                [origin],
                [destination],
                dest_grid_m=dest_grid_m,
                origin_grid_m=origin_grid_m,
            )
        )

    async def walking_matrix_grid(
        self, origins, destinations, dest_grid_m=None, origin_grid_m=None
    ):
        out = [[None] * len(destinations) for _ in origins]
        groups = defaultdict(list)
        # 所有预留在第一个 await 前完成；后续 A/B/设施补测共用同一剩余预算。
        for i, origin in enumerate(origins):
            keep = []
            for j, destination in enumerate(destinations):
                cost = self._cost(origin, destination, dest_grid_m, origin_grid_m)
                if cost and (
                    self.pairs_used + cost > self.pair_limit
                    or self.pair_attempts + cost > self.pair_limit
                ):
                    self.refused_pairs += 1
                    self.refused_points.add((origin, destination, dest_grid_m, origin_grid_m))
                    continue
                self.pairs_used += cost
                keep.append(j)
            if keep:
                groups[tuple(keep)].append(i)
        kwargs = {}
        if dest_grid_m is not None:
            kwargs["dest_grid_m"] = dest_grid_m
        if origin_grid_m is not None:
            kwargs["origin_grid_m"] = origin_grid_m
        for indices, rows in groups.items():
            token = request_observer.set(self._observer(request_observer.get()))
            try:
                measured = await self.client.walking_matrix_grid(
                    [origins[i] for i in rows], [destinations[j] for j in indices], **kwargs
                )
            finally:
                request_observer.reset(token)
            for i, values in zip(rows, measured, strict=True):
                for j, value in zip(indices, values, strict=True):
                    out[i][j] = value
        return out

    async def walking_route(self, origin, destination, fresh=False, fine_m=None):
        probe = getattr(self.client, "walking_route_cached", None)
        cached = not fresh and probe is not None and probe(origin, destination, fine_m=fine_m)
        if not cached:
            if self.routes_used >= self.route_limit or self.route_attempts >= self.route_limit:
                self.refused_routes += 1
                return None
            self.routes_used += 1
        kwargs = {}
        if fine_m is not None:
            kwargs["fine_m"] = fine_m
        if fresh:
            kwargs["fresh"] = True
        token = request_observer.set(self._observer(request_observer.get()))
        try:
            return await self.client.walking_route(origin, destination, **kwargs)
        finally:
            request_observer.reset(token)

    async def route_matrix(self, mode_id, origin, destinations):
        if mode_id != "walk":
            raise ValueError("融合预算客户端仅支持步行")
        rows = await self.walking_matrix_grid([origin], destinations)
        return rows[0] if rows else [None] * len(destinations)

    def summary(self) -> dict:
        return {
            "matrix_pairs": self.pairs_used,
            "routes": self.routes_used,
            "max_pairs": self.max_pairs,
            "max_routes": self.max_routes,
            "refused_pairs": self.refused_pairs,
            "refused_routes": self.refused_routes,
            "pair_attempts": self.pair_attempts,
            "route_attempts": self.route_attempts,
        }
