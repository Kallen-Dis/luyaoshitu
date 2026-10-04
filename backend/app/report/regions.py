"""自动标注设施匮乏的「灰色区域」，并逐片诊断成因。

命题要求「自动标注设施匮乏的灰色区域」。逐格的盲区判定回答的是「这一格走不走得到」，
但规划要面对的是一片一片的区域：把相邻的盲区格合并成连通块，每块给出

- 轮廓（地图上画成一片灰色并加外框）与编号标注（灰色区域 A、B、C……）；
- 面积、格数、多少格落在 15 分钟圈内；
- 缺哪几类、各缺多少格；
- **成因诊断**：对每个缺失的「格子 × 品类」，看直线 1 公里内有没有这类设施——
  - 有，但步行超过 1 公里：**路网阻隔型**。设施就在附近，是铁路、河道、封闭地块
    或缺少过街设施让人绕远，处方应是「打通」；
  - 没有：**供给缺口型**。直线 1 公里内都没有，再怎么修路也到不了，处方应是「补设」。

直线距离在这里只用来区分成因（它是步行距离的下界），盲区本身仍由路网实测判定。
全部由已有结果推导，零 API 消耗。
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from ..isochrone.geometry import METERS_PER_DEG_LAT, bearing_deg
from ..poi.entries import straight_m

# 地图上逐块编号标注的上限；更小的零散块仍然画灰，只是不单独编号
MAX_LABELED = 12

_COMPASS = ("北", "东北", "东", "东南", "南", "西南", "西", "西北")


def _compass(bearing: float) -> str:
    return _COMPASS[int(((bearing % 360) + 22.5) // 45) % 8]


def _letters(i: int) -> str:
    """0 → A，25 → Z，26 → AA。"""
    out = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        out = chr(65 + r) + out
    return out


def _lattice(cells: list[dict[str, Any]], spacing: float):
    """把网格点映射到整数格网坐标。两种布点（圆形 / 多边形内）都是规则格网。"""
    o_lat = float(cells[0]["lat"])
    o_lng = float(cells[0]["lng"])
    d_lat = spacing / METERS_PER_DEG_LAT
    d_lng = spacing / (METERS_PER_DEG_LAT * math.cos(math.radians(o_lat)))

    def index(c: dict[str, Any]) -> tuple[int, int]:
        return (
            round((float(c["lng"]) - o_lng) / d_lng),
            round((float(c["lat"]) - o_lat) / d_lat),
        )

    def to_lnglat(x2: int, y2: int) -> list[float]:
        # 顶点用「两倍格网坐标」表示，格心在偶数、格角在奇数
        return [round(o_lng + x2 / 2 * d_lng, 6), round(o_lat + y2 / 2 * d_lat, 6)]

    return index, to_lnglat


def _components(keys: set[tuple[int, int]]) -> list[list[tuple[int, int]]]:
    """四邻接连通块。"""
    seen: set[tuple[int, int]] = set()
    out: list[list[tuple[int, int]]] = []
    for start in sorted(keys):
        if start in seen:
            continue
        stack = [start]
        seen.add(start)
        block: list[tuple[int, int]] = []
        while stack:
            x, y = stack.pop()
            block.append((x, y))
            for nb in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if nb in keys and nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        out.append(block)
    return out


def _outline(block: list[tuple[int, int]]) -> list[list[tuple[int, int]]]:
    """方格并集的边界环（两倍格网坐标）。相邻两格共享的边方向相反，互相抵消。"""
    edges: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    for x, y in block:
        x0, x1, y0, y1 = 2 * x - 1, 2 * x + 1, 2 * y - 1, 2 * y + 1
        for a, b in (
            ((x0, y0), (x1, y0)),
            ((x1, y0), (x1, y1)),
            ((x1, y1), (x0, y1)),
            ((x0, y1), (x0, y0)),
        ):
            if (b, a) in edges:
                edges.discard((b, a))
            else:
                edges.add((a, b))
    nxt: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
    for a, b in edges:
        nxt[a].append(b)
    rings: list[list[tuple[int, int]]] = []
    while nxt:
        start = next(iter(nxt))
        ring = [start]
        cur = start
        while True:
            ends = nxt.get(cur)
            if not ends:
                break
            b = ends.pop()
            if not ends:
                del nxt[cur]
            if b == start:
                break
            ring.append(b)
            cur = b
        rings.append(_simplify(ring))
    return rings


def _simplify(ring: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """去掉直线上的中间点，只留拐点。"""
    n = len(ring)
    if n < 4:
        return ring
    out = []
    for i in range(n):
        (ax, ay), (bx, by), (cx, cy) = ring[i - 1], ring[i], ring[(i + 1) % n]
        if (bx - ax) * (cy - by) != (by - ay) * (cx - bx):
            out.append(ring[i])
    return out or ring


def _facilities(coverage: dict[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in (coverage or {}).get("places") or []:
        if p.get("lat") is not None and p.get("lng") is not None:
            out[str(p.get("category"))].append(p)
    return out


def classify_missing(
    cells: list[dict[str, Any]],
    coverage: dict[str, Any] | None,
    limit_m: float,
) -> tuple[bool, list[dict[str, dict[str, Any]]]]:
    """逐格、逐个缺失品类判成因。返回 (能否诊断, 每格 {品类: {cause, straight_m, place}})。

    cause：barrier（直线 limit 内就有同类设施，步行却超过 limit）/ supply（直线也没有）/
    unknown（该品类检索失败，或根本没有覆盖统计）。灰色区域、自动诊疗、选址核验共用这一份判定。
    """
    facilities = _facilities(coverage)
    failed = set((coverage or {}).get("failed_categories") or [])
    nearby = (coverage or {}).get("nearby_categories") or {}
    diagnosable = bool(facilities) or bool(coverage and coverage.get("categories"))
    # 附近明明有这类设施、结果里却没有它们的坐标（旧数据、离线模拟）：
    # 不知道在哪，就判不了「直线近不近」，只能记成因未知，不能顺手判成供给缺口
    no_coords = {name for name, n in nearby.items() if n and not facilities.get(name)}
    out: list[dict[str, dict[str, Any]]] = []
    for c in cells:
        entry: dict[str, dict[str, Any]] = {}
        clat, clng = float(c["lat"]), float(c["lng"])
        for name in c.get("missing") or []:
            if not diagnosable or name in failed or name in no_coords:
                entry[name] = {"cause": "unknown", "straight_m": None, "place": None}
                continue
            # 有入口的设施（学校）按最近的入口算直线，和盲区判定的剪枝口径一致
            best = min(
                ((straight_m(clat, clng, p), p) for p in facilities.get(name) or []),
                default=None,
                key=lambda x: x[0],
            )
            entry[name] = {
                "cause": "barrier" if best is not None and best[0] <= limit_m else "supply",
                "straight_m": best[0] if best is not None else None,
                "place": best[1] if best is not None else None,
            }
        out.append(entry)
    return diagnosable, out


def _blocks(cells: list[dict[str, Any]], spacing: float) -> list[list[int]]:
    """盲区格（下标）的四邻接连通块，按格数、圈内格数从大到小排——这就是区域编号的顺序。"""
    index, _ = _lattice(cells, spacing)
    by_key = {index(c): i for i, c in enumerate(cells) if c.get("missing")}
    blocks = [[by_key[k] for k in block] for block in _components(set(by_key))]
    blocks.sort(key=lambda b: (-len(b), -sum(1 for i in b if cells[i].get("in_circle", True))))
    return blocks


def region_labels(blindspots: dict[str, Any] | None) -> dict[int, str | None]:
    """格子下标 → 所在灰色区域编号（A、B……；超出编号上限的零散块为 None）。"""
    cells = list((blindspots or {}).get("cells") or [])
    if not cells:
        return {}
    spacing = float(blindspots.get("grid_spacing_m") or 200.0)
    out: dict[int, str | None] = {}
    for n, block in enumerate(_blocks(cells, spacing)):
        rid = _letters(n) if n < MAX_LABELED else None
        for i in block:
            out[i] = rid
    return out


def gray_regions(
    blindspots: dict[str, Any] | None, coverage: dict[str, Any] | None
) -> dict[str, Any] | None:
    """由网格盲区结果推导灰色区域与成因诊断。没有网格判定时返回 None。"""
    cells = list((blindspots or {}).get("cells") or [])
    if not cells:
        return None
    spacing = float(blindspots.get("grid_spacing_m") or 200.0)
    limit = float(blindspots.get("walk_limit_m") or 1000.0)
    index, to_lnglat = _lattice(cells, spacing)
    blocks = _blocks(cells, spacing)
    if not blocks:
        return {"regions": [], "region_count": 0, "blind_cells": 0, "diagnosable": True}

    diagnosable, causes = classify_missing(cells, coverage, limit)
    cell_km2 = spacing * spacing / 1e6

    regions: list[dict[str, Any]] = []
    for block in blocks:
        members = [cells[i] for i in block]
        missing: dict[str, int] = defaultdict(int)
        barrier: dict[str, int] = defaultdict(int)
        supply: dict[str, int] = defaultdict(int)
        near_example: dict[str, dict[str, Any]] = {}
        for i in block:
            c = cells[i]
            for name in c.get("missing") or []:
                missing[name] += 1
                info = causes[i][name]
                if info["cause"] == "barrier":
                    barrier[name] += 1
                    ex = near_example.get(name)
                    place = info["place"]
                    if ex is None or info["straight_m"] < ex["straight_m"]:
                        near_example[name] = {
                            "straight_m": round(info["straight_m"]),
                            "walk_m": (c.get("nearest_m") or {}).get(name),
                            "place": place.get("name"),
                            "direction": _compass(
                                bearing_deg(
                                    float(c["lat"]),
                                    float(c["lng"]),
                                    float(place["lat"]),
                                    float(place["lng"]),
                                )
                            ),
                        }
                elif info["cause"] == "supply":
                    supply[name] += 1

        lat_c = sum(float(c["lat"]) for c in members) / len(members)
        lng_c = sum(float(c["lng"]) for c in members) / len(members)
        # 标注锚点取离质心最近的那一格，保证标签落在区域里（L 形区域的质心可能在外面）
        anchor = min(
            members,
            key=lambda c: (float(c["lat"]) - lat_c) ** 2 + (float(c["lng"]) - lng_c) ** 2,
        )
        diagnosis = []
        for name, count in sorted(missing.items(), key=lambda kv: -kv[1]):
            b, s = barrier.get(name, 0), supply.get(name, 0)
            if b + s == 0:
                # 该品类检索失败或没有覆盖统计：每一格都判不了成因
                cause = "unknown"
            elif b >= s:
                cause = "barrier"
            else:
                cause = "supply"
            diagnosis.append(
                {
                    "category": name,
                    "cells": count,
                    "barrier_cells": b,
                    "supply_cells": s,
                    "cause": cause,
                    "nearby": near_example.get(name),
                }
            )
        regions.append(
            {
                "cells": len(members),
                "area_km2": round(len(members) * cell_km2, 3),
                "in_circle_cells": sum(1 for c in members if c.get("in_circle", True)),
                "missing": dict(sorted(missing.items(), key=lambda kv: -kv[1])),
                "diagnosis": diagnosis,
                "anchor": {"lat": float(anchor["lat"]), "lng": float(anchor["lng"])},
                "rings": [
                    [to_lnglat(x, y) for x, y in ring]
                    for ring in _outline([index(cells[i]) for i in block])
                ],
            }
        )

    # _blocks 已按格数、圈内格数排好序，编号顺序与 region_labels 一致
    for i, r in enumerate(regions):
        r["id"] = _letters(i) if i < MAX_LABELED else None
        r["label"] = f"灰色区域 {r['id']}" if r["id"] else "零散盲区"
        r["summary"] = _summary(r)
    return {
        "regions": regions,
        "region_count": len(regions),
        "blind_cells": sum(len(b) for b in blocks),
        "diagnosable": diagnosable,
        "basis": (
            "相邻盲区方格合并成片；成因按直线 1 公里内有无同类设施区分：有则路网阻隔，无则供给缺口"
        ),
    }


_CAUSE_TEXT = {"barrier": "路网阻隔", "supply": "供给缺口", "unknown": "成因未知"}


def _summary(region: dict[str, Any]) -> str:
    parts = []
    for d in region["diagnosis"]:
        if d["cause"] == "unknown":
            text = f"{d['category']} {d['cells']} 格（成因未知"
        else:
            split = []
            if d["supply_cells"]:
                split.append(f"供给缺口 {d['supply_cells']} 格")
            if d["barrier_cells"]:
                split.append(f"路网阻隔 {d['barrier_cells']} 格")
            text = f"{d['category']} {d['cells']} 格（{'、'.join(split)}"
        near = d.get("nearby")
        if d["barrier_cells"] and near:
            walk = f"，步行却要 {round(near['walk_m'])} 米" if near.get("walk_m") else ""
            text += (
                f"；往{near['direction']}直线 {near['straight_m']} 米就有"
                f"「{near['place']}」{walk}"
            )
        text += "）"
        parts.append(text)
    # 现版网格只铺在圈内，「其中几格在圈内」就是全部，只有较早的圆形网格结果才需要写
    within = (
        f"，其中 {region['in_circle_cells']} 格在 15 分钟圈内"
        if region["in_circle_cells"] < region["cells"]
        else ""
    )
    head = f"{region['label']}：{region['cells']} 格，约 {region['area_km2']} km²{within}。"
    return head + "缺：" + "；".join(parts) + "。"
