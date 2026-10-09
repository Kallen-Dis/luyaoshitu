"""有共享围挡时惰性核验有向边，选择已核验的可行设施/固定站序行程。"""

from __future__ import annotations

import math

from .candidates import distance, lower_bound
from .planner import combinations_by_bound, initial_layers, solve
from .status import plan_status


async def nearest(service, category, nodes, limit, pending, include_pending, target_id):
    checked = {}
    remaining = list(nodes)
    verified = False
    while remaining:
        batch = remaining[: min(10, service.settings.matrix_batch_size, 50)]
        if not await service.measure([service.origin], batch):
            break
        remaining = remaining[len(batch) :]
        for node in sorted(
            nodes,
            key=lambda n: (
                (service.edges.get((service.origin.id, n.id)) or {}).get("distance_m", math.inf),
                n.id,
            ),
        ):
            edge = service.edges.get((service.origin.id, node.id))
            if edge is None or node.id in checked:
                continue
            clear = unique_clear(checked.values())
            if len(clear) >= limit and edge["distance_m"] > clear[limit - 1]["walk_m"]:
                break
            checked[node.id] = await service.item(service.origin, node, edge, 0)
        clear = unique_clear(checked.values())
        if len(clear) >= limit:
            cutoff = clear[limit - 1]["walk_m"]
            unresolved = [
                n
                for n in nodes
                if n.id not in checked or checked[n.id]["closure_status"] == "unverified"
            ]
            bounds = [
                service.edges[service.origin.id, n.id]["distance_m"]
                if service.edges.get((service.origin.id, n.id)) is not None
                else lower_bound(service.origin, n, service.slack)
                for n in unresolved
            ]
            if not bounds or (not service.bad_bound and cutoff <= min(bounds)):
                verified = not service.simulated
                break
    clear = unique_clear(checked.values())
    blocked = [i for i in checked.values() if i["closure_status"] == "blocked"]
    unknown = [i for i in checked.values() if i["closure_status"] == "unverified"]
    unknown_count = len(unknown) + sum(n.id not in checked for n in nodes)
    if not remaining and not unknown_count:
        verified = not service.simulated
    # 指定目的地不默换店；受阻轨迹供查看，不混入默认可行列表。
    items = clear[:limit]
    if target_id and not items:
        items = sorted(checked.values(), key=lambda i: i["walk_m"])[:1]
    for rank, item in enumerate(items, 1):
        item["rank"] = rank
    if blocked:
        service.warn(f"已跳过 {len(blocked)} 个受阻入口，继续寻找其他设施或入口。")
    if unknown_count:
        service.warn("部分路线未核验，未作为可行路线推荐；预算不足时可能无法补足五家。")
    status = (
        "clear"
        if items and all(i["closure_status"] == "clear" for i in items)
        else "blocked"
        if target_id and blocked
        else "unverified"
        if unknown_count
        else "no_clear_route"
    )
    radius = service.feature["properties"]["coverage"].get("radius_m", 2500)
    range_sufficient = None
    if items and items[-1]["walk_m"] > max(0, radius - service.from_center - service.slack):
        range_sufficient = False
        service.warn("检索范围可能不够，更远处可能还有更近的设施。")
    return {
        **service.summary([category]),
        "category": category,
        "items": items,
        "include_pending": include_pending,
        "pending_count": len(pending),
        "pending_candidates": [
            {**n.as_dict(), "straight_m": round(distance(service.origin, n))} for n in pending[:10]
        ],
        "ranking_verified": verified and status == "clear",
        "verification_basis": "lower_bound_slack_assumption"
        if verified and status == "clear"
        else None,
        "routing_status": status,
        "blocked_count": len(blocked),
        "unverified_count": unknown_count,
        "range_sufficient": range_sufficient,
        "beyond_limit": bool(items and items[0]["walk_m"] > 1000),
    }


def unique_clear(items):
    unique = {}
    for item in sorted(items, key=lambda i: (i["walk_m"], i["entry_id"])):
        if item["closure_status"] == "clear":
            unique.setdefault(item["place_id"], item)
    return list(unique.values())


async def path_items(service, path):
    items = []
    previous = service.origin
    for node in path:
        edge = service.edges.get((previous.id, node.id))
        if edge is None:
            return None
        items.append(await service.item(previous, node, edge, len(items) + 1))
        previous = node
    return items


async def plan(service, stops, layers, selected, replacement, retry_routes=False):
    best = math.inf
    path = []
    legs = []
    unknown_bounds = []
    finished = False
    if selected is not None:
        path = service.selected_path(layers, selected, replacement)
        previous = service.origin
        for node in path:
            if not await service.measure([previous], [node]):
                break
            previous = node
        legs = await path_items(service, path) or []
    else:
        width = min(6, math.isqrt(min(50, service.settings.matrix_batch_size)))
        budget = service.budget.remaining(service.usage)
        available = min(budget[k] for k in ("day_pairs", "hour_pairs", "request_pairs"))
        while width > 1 and width + (len(stops) - 1) * width * width > available:
            width -= 1
        seed = initial_layers(service.origin, layers, width)
        previous = [service.origin]
        for layer in seed:
            if not await service.measure(previous, layer):
                break
            previous = layer
        # DP 先求一个矩阵解，只有其实际路径通过核验才能成为 incumbent。
        allowed = dict(service.edges)
        while True:
            cost, trial = solve(service.origin, seed, allowed)
            if not trial:
                break
            items = await path_items(service, trial)
            if items and all(i["closure_status"] == "clear" for i in items):
                best, path, legs = cost, trial, items
                break
            if items is None:
                break
            for a, b, item in zip([service.origin, *trial], trial, items, strict=False):
                if item["closure_status"] != "clear":
                    allowed[a.id, b.id] = None
                    if item["closure_status"] == "unverified":
                        unknown_bounds.append(0.0)
        try:
            for bound, combo in combinations_by_bound(
                service.origin,
                layers,
                0 if service.bad_bound else service.slack,
                service.settings.trip_search_expansions,
            ):
                if path and not service.bad_bound and bound >= best:
                    finished = True
                    break
                pairs = list(zip([service.origin, *combo], combo, strict=False))
                if any(
                    service.route_items.get((a.id, b.id), {}).get("closure_status") == "blocked"
                    for a, b in pairs
                ):
                    continue
                for a, b in pairs:
                    if not await service.measure([a], [b]):
                        break
                if service.halted:
                    break
                edges = [service.edges.get((a.id, b.id)) for a, b in pairs]
                if any(e is None for e in edges):
                    unknown_bounds.append(bound)
                    continue
                cost = sum(e["distance_m"] for e in edges)
                if cost >= best:
                    continue
                items = await path_items(service, combo)
                if items and all(i["closure_status"] == "clear" for i in items):
                    best, path, legs = cost, combo, items
                elif (
                    items
                    and any(i["closure_status"] == "unverified" for i in items)
                    and not any(i["closure_status"] == "blocked" for i in items)
                ):
                    unknown_bounds.append(bound)
            else:
                finished = True
        except RuntimeError as exc:
            service.warn(str(exc))
    status = plan_status(service.origin.as_dict(), stops, legs, service.basis)
    if not legs:
        service.warn("没有找到已核验的可行行程；可能受阻或预算不足，未生成直线替代路线。")
    proof = (
        finished
        and status == "clear"
        and not service.bad_bound
        and not service.simulated
        and (not unknown_bounds or min(unknown_bounds) >= best)
    )
    alternatives = (
        await service.alternatives(layers, path) if status == "clear" and not retry_routes else []
    )
    return {
        **service.summary(stops),
        "stops": stops,
        "legs": legs,
        "total_m": round(sum(i["walk_m"] for i in legs), 1),
        "total_s": round(sum(i["duration_s"] for i in legs)),
        "alternatives": alternatives,
        "routing_status": status,
        "optimality": "manual"
        if selected is not None
        else "snapshot_tolerance"
        if proof
        else "candidate",
        "verification_basis": "lower_bound_slack_assumption" if proof else None,
    }
