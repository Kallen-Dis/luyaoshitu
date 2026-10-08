"""固定站序 DP、惰性下界组合队列与手动换站；校门始终为独立状态。"""

from __future__ import annotations

import heapq
import itertools
import math
from collections.abc import Iterator

from .candidates import Node, distance, lower_bound

EdgeTable = dict[tuple[str, str], dict | None]


def initial_layers(origin: Node, layers: list[list[Node]], width: int) -> list[list[Node]]:
    previous = [origin]
    out = []
    for layer in layers:
        chosen = sorted(layer, key=lambda n: (min(distance(p, n) for p in previous), n.id))[:width]
        out.append(chosen)
        previous = chosen
    return out


def solve(origin: Node, layers: list[list[Node]], edges: EdgeTable) -> tuple[float, list[Node]]:
    best = {origin.id: (0.0, [])}
    previous = [origin]
    for layer in layers:
        next_best = {}
        for node in layer:
            options = []
            for prev in previous:
                edge = edges.get((prev.id, node.id))
                if edge is not None and prev.id in best:
                    cost, path = best[prev.id]
                    options.append(
                        (cost + edge["distance_m"], (*[p.id for p in path], node.id), [*path, node])
                    )
            if options:
                cost, _, path = min(options, key=lambda v: (v[0], v[1]))
                next_best[node.id] = (cost, path)
        best, previous = next_best, layer
    return min(best.values(), key=lambda v: (v[0], [p.id for p in v[1]]), default=(math.inf, []))


def combinations_by_bound(
    origin: Node, layers: list[list[Node]], slack: float, max_expansions: int
) -> Iterator[tuple[float, list[Node]]]:
    """后缀下界 DP + A* 的纯计算队列，避免物化全部设施组合。"""
    suffix: list[dict[str, float]] = [{} for _ in layers]
    suffix[-1] = {n.id: 0.0 for n in layers[-1]}
    for k in range(len(layers) - 2, -1, -1):
        suffix[k] = {
            n.id: min(lower_bound(n, t, slack) + suffix[k + 1][t.id] for t in layers[k + 1])
            for n in layers[k]
        }
    sequence = itertools.count()
    queue = [(0.0, next(sequence), 0.0, [])]
    created = 1
    while queue:
        bound, _, spent, path = heapq.heappop(queue)
        if len(path) == len(layers):
            yield bound, path
            continue
        k = len(path)
        prev = path[-1] if path else origin
        for node in layers[k]:
            # 限制入队总量；大量同下界候选会先铺满前缀，单限出队次数仍可能耗尽内存。
            if created >= max_expansions:
                raise RuntimeError("候选组合计算达到上限，未完成全量验证。")
            created += 1
            value = spent + lower_bound(prev, node, slack)
            heapq.heappush(
                queue, (value + suffix[k][node.id], next(sequence), value, [*path, node])
            )
