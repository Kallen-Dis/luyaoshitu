"""有预算的步行测距编排；排名只读矩阵距离，折线只负责展示和围挡核验。"""

from __future__ import annotations

import math
from dataclasses import asdict

from ..baidu.client import BaiduMapClient
from ..baidu.errors import QuotaExhaustedError, quota_hint
from ..config import Settings
from ..isochrone.geometry import first_entry_along, offset_point, point_along, polyline_length_m
from ..isochrone.refine import CROSSING_WORDS
from ..poi.catalog import CATEGORIES
from . import feasible
from .budget import BudgetExhausted, TripBudget, Usage
from .candidates import (
    Node,
    TripError,
    blocked,
    candidates,
    distance,
    lower_bound,
    place_id,
    point,
    prepare,
    search_status,
)
from .planner import EdgeTable, combinations_by_bound, initial_layers, solve
from .routing import TripRoutingClient
from .supplement import supplement


def config(settings: Settings) -> dict:
    return {
        "max_stops": 3,
        "max_nearest": 5,
        "matrix_batch_pairs": min(50, max(1, settings.matrix_batch_size)),
        "day_pairs": settings.trip_day_pairs,
        "hour_pairs": settings.trip_hour_pairs,
        "request_pairs": settings.trip_request_pairs,
        "hour_routes": settings.trip_hour_routes,
        "hour_requests": settings.trip_hour_requests,
        "request_routes": settings.trip_request_routes,
        "lower_bound_slack_m": settings.trip_slack_m,
    }


def demo_places(feature: dict) -> None:
    """老的离线模拟没有坐标，仅在规划副本里补上明确命名的模拟设施。"""
    props = feature["properties"]
    coverage = props.get("coverage") or {}
    if not props.get("simulated") or "places" in coverage:
        return
    center = point(props["center"])
    places = []
    for k, category in enumerate(CATEGORIES):
        count = min(8, int(coverage.get("nearby_categories", {}).get(category.name, 3)))
        for j in range(count):
            lat, lng = offset_point(*center, (k * 53 + j * 77) % 360, 250 + j * 145 + k * 28)
            places.append(
                {
                    "category": category.name,
                    "name": f"模拟{category.name}{j+1}",
                    "lat": lat,
                    "lng": lng,
                    "in_circle": True,
                }
            )
    coverage["places"] = places
    props["coverage"] = coverage


class TripService:
    def __init__(
        self, client: BaiduMapClient, feature: dict, raw_origin: dict, *, usage: Usage | None = None
    ) -> None:
        demo_places(feature)
        self.origin, self.from_center, self.closures = prepare(feature, raw_origin)
        self.feature = feature
        self.settings = client._s
        self.usage = usage or Usage()
        self.budget = TripBudget(self.settings.markings_dir / "trip-budget.sqlite3", self.settings)
        center = point(feature["properties"]["center"])
        self.memory_only = raw_origin.get("kind") != "center" or self.origin.coord != center
        self.router = TripRoutingClient(client, self.budget, self.usage, self.memory_only)
        self.simulated = bool(feature["properties"].get("simulated"))
        self.edges: EdgeTable = {}
        self.route_items: dict[tuple[str, str], dict] = {}
        self.slack = self.settings.trip_slack_m
        self.bad_bound = False
        self.halted = False
        self.warnings: list[str] = []
        self.basis = "estimate" if self.simulated else "network"

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def estimate(self, a: Node, b: Node) -> dict:
        raw = self.feature["properties"].get("mean_detour") or 1.25
        factor = float(raw)
        factor = factor if math.isfinite(factor) and factor >= 1 else 1.25
        walk = distance(a, b) * factor
        return {"distance_m": walk, "duration_s": walk / 1.17}

    async def measure(self, origins: list[Node], targets: list[Node]) -> bool:
        if self.simulated:
            for a in origins:
                for b in targets:
                    self.edges[a.id, b.id] = self.estimate(a, b)
            return True
        # 服务层顺序装小矩形，某一块预算不足不丢失已完成块的解。
        cap = config(self.settings)["matrix_batch_pairs"]
        remaining = self.budget.remaining(self.usage)
        cap = max(
            1, min(cap, *(remaining[k] for k in ("day_pairs", "hour_pairs", "request_pairs")))
        )
        d_size = min(len(targets), cap)
        if not d_size:
            return True
        o_size = max(1, cap // d_size)
        for oi in range(0, len(origins), o_size):
            for di in range(0, len(targets), d_size):
                rows, cols = origins[oi : oi + o_size], targets[di : di + d_size]
                if all((a.id, b.id) in self.edges for a in rows for b in cols):
                    continue
                try:
                    grid = await self.router.walking_matrix_grid(
                        [a.coord for a in rows], [b.coord for b in cols]
                    )
                except (BudgetExhausted, QuotaExhaustedError) as exc:
                    self.halted = True
                    self.warn(quota_hint(exc) if isinstance(exc, QuotaExhaustedError) else str(exc))
                    return False
                for i, a in enumerate(rows):
                    for j, b in enumerate(cols):
                        edge = grid[i][j]
                        self.edges[a.id, b.id] = edge
                        if edge is None:
                            self.warn("部分候选测距失败，记为未知，未用失败点证明排序或最优性。")
                        elif edge["distance_m"] < distance(a, b) - self.slack:
                            self.bad_bound = True
                            self.warn("实测端点偏差超过下界容差，已停止用该下界证明排序。")
        return True

    def summary(self, categories: list[str]) -> dict:
        status = search_status(self.feature, categories)
        if status in ("unknown", "truncated"):
            self.warn(
                "设施检索完整性未知，结果仅针对已检索设施。"
                if status == "unknown"
                else "部分设施关键词检索被截断，结果仅针对已检索设施。"
            )
        if self.basis != "network":
            self.warn("直线估算，未测路网；虚线不代表可以沿直线通行。")
        return {
            "origin": {
                "lat": self.origin.lat,
                "lng": self.origin.lng,
                "from_center_m": round(self.from_center),
            },
            "basis": self.basis,
            "search_status": status,
            "lower_bound_slack_m": self.slack,
            "quota": {**asdict(self.usage), "remaining": self.budget.remaining(self.usage)},
            "warnings": list(self.warnings),
            "cache_policy": "memory" if self.memory_only else "disk",
        }

    async def item(self, a: Node, b: Node, edge: dict, rank: int) -> dict:
        key = (a.id, b.id)
        if key not in self.route_items:
            self.route_items[key] = await self._item(a, b, edge, rank)
        return {**self.route_items[key], "rank": rank}

    async def _item(self, a: Node, b: Node, edge: dict, rank: int) -> dict:
        straight = distance(a, b)
        route = None
        note = None
        closure_status = "unverified"
        if blocked(a.coord, self.closures):
            closure_status = "blocked"
            note = "起点位于已知围挡内，无法确认可通行。"
        if self.basis == "network" and closure_status != "blocked":
            try:
                raw = await self.router.walking_route(a.coord, b.coord)
            except BudgetExhausted as exc:
                raw = None
                note = str(exc)
            if raw:
                points = [tuple(p) for step in raw.get("steps", []) for p in step.get("path", [])]
                if len(points) >= 2:
                    circles = [(c["lat"], c["lng"], c["radius_m"]) for c in self.closures]
                    entry = first_entry_along(points, circles)
                    closure_status = "blocked" if entry is not None else "clear"
                    crossing = {"过街": 0, "天桥": 0, "地道": 0}
                    for step in raw.get("steps", []):
                        for word, kind in CROSSING_WORDS:
                            if word in step.get("instruction", ""):
                                crossing[kind] += 1
                                break
                    blocked_path = []
                    closure_entry = None
                    if entry is not None:
                        lat, lng = point_along(points, entry)
                        closure_entry = {"lat": lat, "lng": lng, "at_m": entry}
                        walked = 0.0
                        blocked_path = [[lng, lat]]
                        for i in range(1, len(points)):
                            walked += distance_points(points[i - 1], points[i])
                            if walked >= entry:
                                blocked_path.append([points[i][1], points[i][0]])
                        note = "路线受施工围挡阻断，不能确认可通行，建议换一家；接口不能自动绕开。"
                    connectors = []
                    if distance_points(a.coord, points[0]) > 1:
                        connectors.append([[a.lng, a.lat], [points[0][1], points[0][0]]])
                    if distance_points(b.coord, points[-1]) > 1:
                        connectors.append([[points[-1][1], points[-1][0]], [b.lng, b.lat]])
                    route = {
                        "distance_m": raw["distance_m"],
                        "duration_s": raw["duration_s"],
                        "path": [[lng, lat] for lat, lng in points],
                        "steps": [
                            {
                                **{k: v for k, v in s.items() if k != "path"},
                                "path": [[lng, lat] for lat, lng in s.get("path", [])],
                            }
                            for s in raw.get("steps", [])
                        ],
                        "crossings": crossing,
                        "closure_entry": closure_entry,
                        "blocked_path": blocked_path,
                        "connectors": connectors,
                    }
            if route is None:
                quota = self.router.exhausted("/directionlite/v1/walking")
                if quota:
                    note = quota_hint(quota)
                note = note or "路线没取到，距离来自批量算路；耗时未含过街等待，围挡影响未核验。"
        return {
            **b.as_dict(),
            # 入口坐标可能远离POI中心；标记整个设施失效要使用原设施位置。
            "place": next(
                (
                    {**p, "id": b.place_id}
                    for p in self.feature["properties"].get("coverage", {}).get("places", [])
                    if place_id(p) == b.place_id
                ),
                None,
            ),
            "rank": rank,
            "straight_m": round(straight, 1),
            "walk_m": round(edge["distance_m"], 1),
            "duration_s": route["duration_s"] if route else edge["duration_s"],
            "duration_basis": "route"
            if route
            else "matrix"
            if self.basis == "network"
            else "estimate",
            "detour": round(edge["distance_m"] / straight, 2) if straight > 0 else None,
            "route": route,
            "closure_status": closure_status,
            "note": note,
            "from": a.as_dict(),
        }

    async def nearest(
        self,
        category: str,
        limit: int,
        target_id: str | None = None,
        include_pending: bool = False,
        poi_query: str | None = None,
        refresh_pois: bool = True,
    ) -> dict:
        if refresh_pois:
            for warning in await supplement(self.router.base, self.feature, [category], poi_query):
                self.warn(warning)
        all_nodes = candidates(self.feature, category, self.closures, include_pending=True)
        pending = sorted(
            [n for n in all_nodes if n.fresh_status == "pending"],
            key=lambda n: distance(self.origin, n),
        )
        nodes = candidates(
            self.feature,
            category,
            self.closures,
            include_pending=include_pending or bool(target_id),
        )
        if pending:
            self.warn("附近还有是否卖菜待确认的超市；默认排名只含已核实及规则推定门店。")
        if target_id:
            nodes = [n for n in nodes if n.place_id == target_id]
            if not nodes:
                raise TripError("invalid", "指定设施不在当前快照中，或其入口位于围挡内。")
            limit = 1
        nodes.sort(key=lambda n: (distance(self.origin, n), n.id))
        if self.closures:
            return await feasible.nearest(
                self, category, nodes, limit, pending, include_pending, target_id
            )
        unresolved = list(nodes)
        verified = False
        verification = None
        winners: list[tuple[float, Node]] = []
        while unresolved:
            batch = unresolved[: min(10, config(self.settings)["matrix_batch_pairs"])]
            if not await self.measure([self.origin], batch):
                break
            unresolved = unresolved[len(batch) :]
            winners = self.nearest_winners(nodes)
            unknown = [n for n in nodes if self.edges.get((self.origin.id, n.id)) is None]
            if not unknown:
                verified = not self.simulated
                verification = "all_snapshot_candidates" if verified else None
                break
            if (
                not self.simulated
                and not self.bad_bound
                and len(winners) >= limit
                and winners[limit - 1][0]
                <= min(lower_bound(self.origin, n, self.slack) for n in unknown)
            ):
                verified, verification = True, "lower_bound_slack_assumption"
                break
        winners = self.nearest_winners(nodes)
        if not winners and nodes:
            self.basis = (
                "estimate"
                if self.simulated
                else "estimate_quota"
                if self.halted
                else "estimate_error"
            )
            winners = sorted(
                [(self.estimate(self.origin, n)["distance_m"], n) for n in nodes],
                key=lambda v: (v[0], v[1].id),
            )
            unique = {}
            for value, n in winners:
                unique.setdefault(n.place_id, (value, n))
            winners = list(unique.values())
        items = []
        for _, n in winners[:limit]:
            edge = self.edges.get((self.origin.id, n.id)) or self.estimate(self.origin, n)
            items.append(await self.item(self.origin, n, edge, len(items) + 1))
        if nodes and not verified and not self.simulated:
            self.warn(
                "当前已测候选中的最近几家，尚未完成排序验证。"
                if self.basis == "network"
                else "按直线估算展示候选，未验证路网排名。"
            )
        if not nodes:
            radius_km = self.feature["properties"]["coverage"].get("radius_m", 2500) / 1000
            self.warn(f"分析中心 {radius_km:g} 公里内没有检索到可用的这一类设施。")
        range_sufficient = None
        if items:
            radius = self.feature["properties"]["coverage"].get("radius_m", 2500)
            if items[-1]["walk_m"] > max(0, radius - self.from_center - self.slack):
                range_sufficient = False
                self.warn("检索范围可能不够，更远处可能还有更近的设施。")
        return {
            **self.summary([category]),
            "category": category,
            "items": items,
            "include_pending": include_pending,
            "pending_count": len(pending),
            "pending_candidates": [
                {**n.as_dict(), "straight_m": round(distance(self.origin, n))} for n in pending[:10]
            ],
            "ranking_verified": verified,
            "verification_basis": verification,
            "range_sufficient": range_sufficient,
            "beyond_limit": bool(items and items[0]["walk_m"] > 1000),
        }

    def nearest_winners(self, nodes: list[Node]) -> list[tuple[float, Node]]:
        chosen = {}
        for n in nodes:
            edge = self.edges.get((self.origin.id, n.id))
            if edge is not None:
                value = (edge["distance_m"], n)
                current = chosen.get(n.place_id)
                if current is None or (value[0], n.id) < (current[0], current[1].id):
                    chosen[n.place_id] = value
        return sorted(chosen.values(), key=lambda v: (v[0], v[1].id))

    async def reanchor(self, stops: list[str], selected: list[dict]) -> dict:
        """固定每站设施及入口；只测所选有向边，不搜索组合或生成备选。"""
        layers = [candidates(self.feature, c, self.closures, include_pending=True) for c in stops]
        path = self.selected_path(layers, selected, None)
        legs = []
        previous = self.origin
        for node in path:
            if not await self.measure([previous], [node]):
                break
            edge = self.edges.get((previous.id, node.id))
            if edge is None:
                self.warn("重新测距失败，原计划保留。")
                break
            item = await self.item(previous, node, edge, len(legs) + 1)
            legs.append(item)
            if item["closure_status"] != "clear" or not item["route"]:
                break
            previous = node
        status = (
            "clear"
            if len(legs) == len(stops) and all(i["closure_status"] == "clear" for i in legs)
            else "blocked"
            if any(i["closure_status"] == "blocked" for i in legs)
            else "unverified"
        )
        if status != "clear":
            self.warn("新行程未通过通行核验，未替换原计划。")
        return {
            **self.summary(stops),
            "stops": stops,
            "legs": legs,
            "total_m": round(sum(i["walk_m"] for i in legs), 1),
            "total_s": round(sum(i["duration_s"] for i in legs)),
            "alternatives": [],
            "optimality": "manual",
            "verification_basis": None,
            "routing_status": status,
        }

    async def plan(
        self, stops: list[str], selected: list[dict] | None = None, replace: dict | None = None
    ) -> dict:
        if len(stops) == 1 and selected is None:
            result = await self.nearest(stops[0], 1)
            items = result.pop("items")
            return {
                **result,
                "stops": stops,
                "legs": items,
                "total_m": sum(i["walk_m"] for i in items),
                "total_s": sum(i["duration_s"] for i in items),
                "alternatives": [],
                "optimality": (
                    "snapshot_measured"
                    if result["verification_basis"] == "all_snapshot_candidates"
                    else "snapshot_tolerance"
                    if result["ranking_verified"]
                    else "candidate"
                ),
            }
        for warning in await supplement(self.router.base, self.feature, stops):
            self.warn(warning)
        layers = [candidates(self.feature, c, self.closures) for c in stops]
        if any(not layer for layer in layers):
            self.warn("至少一站没有检索到可用设施，无法组成行程。")
            return {
                **self.summary(stops),
                "stops": stops,
                "legs": [],
                "total_m": 0,
                "total_s": 0,
                "alternatives": [],
                "optimality": "candidate",
                "verification_basis": None,
            }
        optimality = "candidate"
        verification = None
        if self.closures:
            return await feasible.plan(self, stops, layers, selected, replace)
        if selected is not None:
            path = self.selected_path(layers, selected, replace)
            prev = self.origin
            for n in path:
                if not await self.measure([prev], [n]):
                    break
                prev = n
            if any(
                self.edges.get((a.id, b.id)) is None
                for a, b in zip([self.origin, *path], path, strict=False)
            ):
                # 不允许手动替换请求悄悄把已选行程改成别的站点。
                self.basis = (
                    "estimate"
                    if self.simulated
                    else "estimate_quota"
                    if self.halted
                    else "estimate_error"
                )
            optimality = "manual"
        else:
            width = min(6, math.isqrt(config(self.settings)["matrix_batch_pairs"]))
            remaining = self.budget.remaining(self.usage)
            seed_budget = min(remaining[k] for k in ("day_pairs", "hour_pairs", "request_pairs"))
            # 初始预算包含起点→第一层，而不只是两层之间的矩形。
            while width > 1 and width + (len(stops) - 1) * width * width > seed_budget:
                width -= 1
            initial = initial_layers(self.origin, layers, width)
            previous = [self.origin]
            for layer in initial:
                if not await self.measure(previous, layer):
                    break
                previous = layer
            best, path = solve(self.origin, initial, self.edges)
            unknown_bounds = []
            finished = False
            if not self.halted:
                try:
                    for bound, combo in combinations_by_bound(
                        self.origin,
                        layers,
                        0 if self.bad_bound else self.slack,
                        self.settings.trip_search_expansions,
                    ):
                        if path and not self.bad_bound and not self.simulated and bound >= best:
                            finished = True
                            break
                        previous = self.origin
                        for n in combo:
                            if not await self.measure([previous], [n]):
                                break
                            previous = n
                        if self.halted:
                            break
                        values = [
                            self.edges.get((a.id, b.id))
                            for a, b in zip([self.origin, *combo], combo, strict=False)
                        ]
                        if any(v is None for v in values):
                            unknown_bounds.append(bound)
                            continue
                        value = sum(v["distance_m"] for v in values)
                        if value < best:
                            best, path = value, combo
                    else:
                        finished = True
                except RuntimeError as exc:
                    self.warn(str(exc))
            if (
                finished
                and path
                and not self.simulated
                and (not unknown_bounds or min(unknown_bounds) >= best)
            ):
                if self.bad_bound:
                    # 偏差失效后只有所有组合实际测清才能证明；不能用旧队列的下界结束。
                    if not unknown_bounds:
                        optimality, verification = "snapshot_measured", "all_snapshot_candidates"
                else:
                    optimality, verification = "snapshot_tolerance", "lower_bound_slack_assumption"
            if not path:
                self.basis = (
                    "estimate"
                    if self.simulated
                    else "estimate_quota"
                    if self.halted
                    else "estimate_error"
                )
                previous = self.origin
                path = []
                for layer in layers:
                    n = min(layer, key=lambda n: (distance(previous, n), n.id))
                    path.append(n)
                    previous = n
            if optimality == "candidate":
                self.warn(
                    "候选内最短行程，未完成全部已检索候选的最优性验证。"
                    if self.basis == "network"
                    else "按直线估算生成行程，未验证路网距离或最优性。"
                )
        legs = []
        previous = self.origin
        for n in path:
            edge = self.edges.get((previous.id, n.id)) if self.basis == "network" else None
            legs.append(
                await self.item(previous, n, edge or self.estimate(previous, n), len(legs) + 1)
            )
            previous = n
        alternatives = await self.alternatives(layers, path)
        if self.bad_bound and optimality == "snapshot_tolerance":
            optimality, verification = "candidate", None
            self.warn("候选内最短行程，未完成全部已检索候选的最优性验证。")
        return {
            **self.summary(stops),
            "stops": stops,
            "legs": legs,
            "total_m": round(sum(i["walk_m"] for i in legs), 1),
            "total_s": round(sum(i["duration_s"] for i in legs)),
            "alternatives": alternatives,
            "optimality": optimality,
            "verification_basis": verification,
        }

    def selected_path(
        self, layers: list[list[Node]], selected: list[dict], replace: dict | None
    ) -> list[Node]:
        if len(selected) != len(layers):
            raise TripError("invalid", "已选站点与站数不一致。")
        selectors = list(selected)
        if replace:
            index = replace.get("index")
            if not isinstance(index, int) or not 0 <= index < len(layers):
                raise TripError("invalid", "替换站点索引无效。")
            selectors[index] = replace
        path = []
        for layer, selector in zip(layers, selectors, strict=True):
            node = next(
                (
                    n
                    for n in layer
                    if n.id == selector.get("entry_id") and n.place_id == selector.get("place_id")
                ),
                None,
            )
            if node is None:
                raise TripError("invalid", "所选设施或校门不在当前这一站的快照候选中。")
            path.append(node)
        return path

    async def alternatives(self, layers: list[list[Node]], path: list[Node]) -> list[dict]:
        if self.basis != "network" and not self.simulated:
            # 没有完整路网解时不把实测相邻边与估算邻边混成备选总距离。
            return [{"index": i, "items": []} for i in range(len(layers))]
        out = []
        for index, layer in enumerate(layers):
            prev = path[index - 1] if index else self.origin
            following = path[index + 1] if index + 1 < len(path) else None
            options = []
            attempted_places: set[str] = set()
            for n in sorted(layer, key=lambda n: distance(prev, n)):
                if n.place_id == path[index].place_id:
                    continue
                if n.place_id not in attempted_places:
                    if len(attempted_places) >= 3:
                        continue
                    attempted_places.add(n.place_id)
                if self.basis == "network" and not self.halted:
                    await self.measure([prev], [n])
                    if following and not self.halted:
                        await self.measure([n], [following])
                edge = self.edges.get((prev.id, n.id))
                tail = self.edges.get((n.id, following.id)) if following else {"distance_m": 0}
                if self.simulated:
                    edge, tail = (
                        self.estimate(prev, n),
                        self.estimate(n, following) if following else {"distance_m": 0},
                    )
                if edge is None or tail is None:
                    continue
                if self.closures:
                    incoming = await self.item(prev, n, edge, 0)
                    outgoing = await self.item(n, following, tail, 0) if following else None
                    if incoming["closure_status"] != "clear" or (
                        outgoing and outgoing["closure_status"] != "clear"
                    ):
                        continue
                fixed = 0.0
                for j, current in enumerate(path):
                    if j in (index, index + 1):
                        continue
                    origin = path[j - 1] if j else self.origin
                    fixed += (
                        self.edges.get((origin.id, current.id)) or self.estimate(origin, current)
                    )["distance_m"]
                options.append(
                    {
                        **n.as_dict(),
                        "total_m": round(fixed + edge["distance_m"] + tail["distance_m"], 1),
                    }
                )
            unique = {}
            for opt in sorted(options, key=lambda o: (o["total_m"], o["entry_id"])):
                unique.setdefault(opt["place_id"], opt)
            out.append({"index": index, "items": list(unique.values())[:3]})
        return out


def distance_points(a: tuple, b: tuple) -> float:
    return polyline_length_m([a, b])
