"""50 米叠加细分的派生视图：把「100 米父格 + 部分格的 4 个子格」展开成一张均匀的 50 米格网。

盲区结果里存的是叠加式结构（blindspot.py）：每个 100 米格保留自己格心的结论，
细分过的格另带 4 个子格（quarters）。灰色区域、评分、处方、模拟新建要的是一张均匀格网，
所以在这里统一展开：

- 细分过的格：4 个子格用各自的结论（实测过的品类是实测，其余品类继承父格）；
  子格中心落在射线圈外的（in_circle = false）不进视图，父格只剩圈内子格的面积；
- 没细分的格：也切成 4 个子格，全部继承父格结论（source = inherited），按父格心在圈内整格计。

每个子格带 weight = 0.25：所有计数都按 100 米格折算（子格面积替代父格面积，不重复统计），
所以「灰色区域 A 14.75 格」与以前的「15 格」是同一个单位。没有任何子格时原样返回，
旧结果的数字一点不变。
"""

from __future__ import annotations

from typing import Any

from ..isochrone.geometry import METERS_PER_DEG_LAT, meters_per_deg_lng

QUARTER_OFFSETS = (("NW", 1, -1), ("NE", 1, 1), ("SW", -1, -1), ("SE", -1, 1))
PARENT_FIELDS = ("reach_s", "reach_raw_s", "reach_m", "in_circle", "closure_blocked")


def weight(cell: dict[str, Any]) -> float:
    return float(cell.get("weight", 1.0))


def total(cells: list[dict[str, Any]], indices=None) -> float:
    """按 100 米格折算的格数。"""
    items = cells if indices is None else (cells[i] for i in indices)
    return sum(weight(c) for c in items)


def fmt(n: float) -> str:
    """格数的写法：整数不带小数，细分出来的 1/4 格保留到 0.25。"""
    n = round(n * 4) / 4
    return str(int(n)) if n == int(n) else f"{n:g}"


def is_refined(blindspots: dict[str, Any] | None) -> bool:
    return bool(blindspots) and any(c.get("quarters") for c in blindspots.get("cells") or [])


def refined_view(blindspots: dict[str, Any] | None) -> dict[str, Any] | None:
    if not blindspots or blindspots.get("view") == "refined" or not is_refined(blindspots):
        return blindspots
    cells = list(blindspots.get("cells") or [])
    spacing = float(blindspots.get("grid_spacing_m") or 100.0)
    half = spacing / 4
    mean_lat = sum(float(c["lat"]) for c in cells) / len(cells)
    dlat = half / METERS_PER_DEG_LAT
    dlng = half / meters_per_deg_lng(mean_lat)
    out: list[dict[str, Any]] = []
    for i, c in enumerate(cells):
        lat, lng = float(c["lat"]), float(c["lng"])
        by_q = {q.get("q"): q for q in c.get("quarters") or []}
        for label, sy, sx in QUARTER_OFFSETS:
            q = by_q.get(label)
            if q is not None and q.get("in_circle") is False:
                continue  # 子格中心在射线圈外：不测、不计，父格只剩圈内子格的面积
            src = q or c
            out.append(
                {
                    **{k: c.get(k) for k in PARENT_FIELDS if k in c},
                    "lat": float(q["lat"]) if q else lat + sy * dlat,
                    "lng": float(q["lng"]) if q else lng + sx * dlng,
                    "nearest_m": dict(src.get("nearest_m") or {}),
                    "missing": list(src.get("missing") or []),
                    "unknown": list(src.get("unknown") or []),
                    "unknown_reasons": dict(src.get("unknown_reasons") or {}),
                    "weight": 0.25,
                    "parent": i,
                    "parent_at": [lat, lng],
                    "q": label,
                    "split": bool(by_q),
                    "measured": list((q or {}).get("measured") or []),
                }
            )
    return {
        **blindspots,
        "view": "refined",
        "cells": out,
        "grid_spacing_m": spacing / 2,
        "base_spacing_m": spacing,
        "blind_count": round(sum(0.25 for c in out if c["missing"]), 2),
    }


def base_spacing(blindspots: dict[str, Any] | None, default: float = 100.0) -> float:
    grid = blindspots or {}
    return float(grid.get("base_spacing_m") or grid.get("grid_spacing_m") or default)


def recount(blindspots: dict[str, Any], cells: list[dict[str, Any]]) -> dict[str, Any]:
    """按新的逐格判定重算盲区面积与各品类占比（按面积，测距失败的格不进分母）。"""
    out = dict(blindspots)
    out["cells"] = cells
    out["blind_count"] = round(sum(weight(c) for c in cells if c.get("missing")), 2)
    names = set(blindspots.get("blind_ratio") or {})
    for c in cells:
        names.update(c.get("missing") or [])
    # 判定不足、本来就没给占比的品类保持不给，否则模拟前后的评分口径不一致
    names -= set(blindspots.get("incomplete_categories") or [])
    ratio: dict[str, float] = {}
    for name in sorted(names):
        judged = [c for c in cells if name not in (c.get("unknown") or [])]
        w = sum(weight(c) for c in judged)
        if w > 0:
            blind = sum(weight(c) for c in judged if name in (c.get("missing") or []))
            ratio[name] = round(blind / w, 3)
    out["blind_ratio"] = ratio
    return out


def origin_groups(
    view_cells: list[dict[str, Any]], indices: list[int], category: str
) -> tuple[dict[tuple[float, float], list[int]], dict[tuple[float, float], list[int]]]:
    """要从哪些点出发测距：这一品类实测过的子格从子格中心出发（起点细键），
    继承父格结论的子格从父格心出发（同一父格的几个子格共用一次测距）。

    返回 (父格心 → 子格下标, 子格中心 → 子格下标)。没有细分时每格就是父格心。
    """
    parents: dict[tuple[float, float], list[int]] = {}
    quarters: dict[tuple[float, float], list[int]] = {}
    for i in indices:
        c = view_cells[i]
        if category in (c.get("measured") or []):
            quarters.setdefault((float(c["lat"]), float(c["lng"])), []).append(i)
        elif c.get("parent_at"):
            at = c["parent_at"]
            parents.setdefault((float(at[0]), float(at[1])), []).append(i)
        else:
            parents.setdefault((float(c["lat"]), float(c["lng"])), []).append(i)
    return parents, quarters


def display_key(cell: dict[str, Any]) -> tuple[float, float]:
    """前端按哪个坐标认这一格：细分过的父格画成 4 个子格，用子格中心；没细分的画父格，用父格心。"""
    if cell.get("split") or not cell.get("parent_at"):
        return float(cell["lat"]), float(cell["lng"])
    at = cell["parent_at"]
    return float(at[0]), float(at[1])
