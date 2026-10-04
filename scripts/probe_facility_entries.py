"""对照实验：小学按「校门」测距 vs 按地图上的一个点测距，盲区结论差多少。

学校有面积，地点检索给的坐标常在校园中间。快照里的小学已经带着入口（导航点 + 校门，
见 backend/app/poi/entries.py）。这里挑出缺小学、或「刚好够得着」（最近的小学步行 ≥ 850 米）的方格，
把直线 1 公里内每所小学的坐标点和入口各测一遍，比较两种口径的判定。

坐标点按 50 米粗键缓存、入口按 10 米细键缓存，和生产代码一致；测过的点对不再花配额。

    python scripts/probe_facility_entries.py --dry-run         # 只数要实发多少点对
    python scripts/probe_facility_entries.py --write           # 测完写 reports/school-gates.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

from _singleton import AlreadyRunning, single_instance

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.baidu.client import BaiduMapClient  # noqa: E402
from app.isochrone.geometry import haversine_m  # noqa: E402
from app.poi.entries import ENTRY_GRID_M, entry_points  # noqa: E402
from app.report.regions import region_labels  # noqa: E402
from app.travel import get_mode  # noqa: E402

SAMPLES = ROOT / "data" / "samples"
REPORT = ROOT / "reports" / "school-gates.md"
CATEGORY = "基础教育"
LIMIT = 1000.0
BORDERLINE = 850.0  # 判为够得着、但最近的小学步行超过这么远的方格也测：两种口径可能在这里分歧


def load(key: str) -> dict[str, Any]:
    return json.loads((SAMPLES / f"isochrone-{key}-15min.geojson").read_text(encoding="utf-8"))


def plan(feature: dict[str, Any]):
    p = feature["properties"]
    cells = p["blindspots"]["cells"]
    schools = [x for x in p["coverage"]["places"] if x["category"] == CATEGORY]
    test = [
        i
        for i, c in enumerate(cells)
        if CATEGORY not in (c.get("unknown") or [])
        and (
            CATEGORY in (c.get("missing") or [])
            or ((c.get("nearest_m") or {}).get(CATEGORY) or 0) >= BORDERLINE
        )
    ]
    jobs = []  # (学校, 要测的方格, 坐标点, 入口)
    for s in schools:
        point = (float(s["lat"]), float(s["lng"]))
        gates = entry_points(s)

        def straight(i: int, dests=(point, *gates)) -> float:
            c = cells[i]
            return min(haversine_m(c["lat"], c["lng"], d[0], d[1]) for d in dests)

        near = [i for i in test if straight(i) <= LIMIT]
        if near:
            jobs.append((s, near, point, gates))
    return cells, schools, test, jobs


def count_uncached(client: BaiduMapClient, cells, jobs) -> tuple[int, int]:
    mode = get_mode("walk")
    ttl = client._ttl(mode)
    total = missing = 0
    for _, near, point, gates in jobs:
        for i in near:
            o = (cells[i]["lat"], cells[i]["lng"])
            for d, fine in [(point, None)] + [(g, ENTRY_GRID_M) for g in gates]:
                total += 1
                if client._cache.read(client._pair_key(mode, o, d, fine), ttl) is None:
                    missing += 1
    return total, missing


async def run(key: str, dry_run: bool, budget: int) -> dict[str, Any]:
    feature = load(key)
    p = feature["properties"]
    name = p["name"].replace("上海市普陀区", "")
    cells, schools, test, jobs = plan(feature)
    async with BaiduMapClient() as client:
        total, missing = count_uncached(client, cells, jobs)
        print(
            f"[{name}] 测 {len(test)} 格、{len(jobs)} 所小学：点对 {total} 个，"
            f"其中要实发 {missing} 个",
            flush=True,
        )
        if dry_run:
            return {"name": name, "pairs_needed": missing}
        if missing > budget:
            raise SystemExit(f"要实发 {missing} 个点对，超过预算 {budget}（--budget 调高）")
        before = client.matrix_pairs
        point_d: dict[int, float] = {}
        gate_d: dict[int, float] = {}
        gate_via: dict[int, str] = {}
        deltas: list[float] = []
        for s, near, point, gates in jobs:
            origins = [(cells[i]["lat"], cells[i]["lng"]) for i in near]
            by_point = await client.walking_matrix_grid(origins, [point])
            by_gate = (
                await client.walking_matrix_grid(origins, gates, dest_grid_m=ENTRY_GRID_M)
                if gates
                else by_point
            )
            for i, pr, gr in zip(near, by_point, by_gate, strict=True):
                pd = pr[0]["distance_m"] if pr[0] else math.inf
                gd = min((e["distance_m"] for e in gr if e), default=math.inf)
                point_d[i] = min(point_d.get(i, math.inf), pd)
                if gd < gate_d.get(i, math.inf):
                    gate_d[i] = gd
                    gate_via[i] = s["name"]
                if gates and math.isfinite(pd) and math.isfinite(gd):
                    deltas.append(gd - pd)
        pairs = client.matrix_pairs - before

    labels = region_labels(p["blindspots"])
    flips = {"缺 → 够得着": [], "够得着 → 缺": []}
    for i in test:
        old = point_d.get(i, math.inf) > LIMIT
        new = gate_d.get(i, math.inf) > LIMIT
        if old and not new:
            flips["缺 → 够得着"].append(i)
        elif new and not old:
            flips["够得着 → 缺"].append(i)
    deltas.sort()
    gaps = [
        haversine_m(float(s["lat"]), float(s["lng"]), e["lat"], e["lng"])
        for s in schools
        for e in s.get("entries") or []
    ]
    examples = sorted(
        (
            (gate_via.get(i), round(point_d[i]), round(gate_d[i]))
            for i in flips["缺 → 够得着"] + flips["够得着 → 缺"]
            if math.isfinite(point_d.get(i, math.inf)) and math.isfinite(gate_d.get(i, math.inf))
        ),
        key=lambda x: x[2] - x[1],
    )
    return {
        "name": name,
        "schools": len(schools),
        "with_gates": sum(
            1 for s in schools if any(e["name"] != "导航点" for e in s.get("entries") or [])
        ),
        "with_entries": sum(1 for s in schools if s.get("entries")),
        "entries": sum(len(s.get("entries") or []) for s in schools),
        "entry_gap_m": (round(min(gaps)), round(sorted(gaps)[len(gaps) // 2]), round(max(gaps)))
        if gaps
        else None,
        "tested": len(test),
        "point_missing": sum(1 for i in test if point_d.get(i, math.inf) > LIMIT),
        "gate_missing": sum(1 for i in test if gate_d.get(i, math.inf) > LIMIT),
        "to_ok": len(flips["缺 → 够得着"]),
        "to_missing": len(flips["够得着 → 缺"]),
        "to_ok_regions": sorted({labels.get(i) or "零散" for i in flips["缺 → 够得着"]}),
        "delta_m": (round(deltas[0]), round(deltas[len(deltas) // 2]), round(deltas[-1]))
        if deltas
        else None,
        "examples": examples[:3] + examples[-2:] if len(examples) > 5 else examples,
        "pairs": pairs,
        "pairs_total": total,
    }


def render(results: list[dict[str, Any]]) -> str:
    L = ["# 小学按校门测距：对照实验", ""]
    L.append(
        f"> 由 `scripts/probe_facility_entries.py` 生成（{time.strftime('%Y-%m-%d')}）。"
        "同一批方格、同一批小学，一种口径测到地图上的坐标点，一种测到入口（导航点 + 校门），"
        "比较「步行 1 公里内有没有小学」的判定。"
    )
    L.append("")
    L.append(
        "测的方格：现版快照里缺小学的，以及最近的小学步行 850 米以上、刚好够得着的。"
        "每格测直线 1 公里内的每一所小学。"
    )
    L.append("")
    L.append(
        "| 样例 | 小学（有入口 / 有校门） | 入口离坐标点 | 测的方格 | 按点：缺 | 按门：缺 | "
        "缺 → 够得着 | 够得着 → 缺 | 按门比按点（中位数） |"
    )
    L.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in results:
        gap = r["entry_gap_m"]
        delta = r["delta_m"]
        L.append(
            f"| {r['name']} | {r['schools']}（{r['with_entries']} / {r['with_gates']}） | "
            f"{f'{gap[0]}~{gap[2]} 米，中位 {gap[1]}' if gap else '—'} | {r['tested']} | "
            f"{r['point_missing']} | {r['gate_missing']} | {r['to_ok']} | {r['to_missing']} | "
            f"{f'{delta[1]:+d} 米（{delta[0]:+d} ~ {delta[2]:+d}）' if delta else '—'} |"
        )
    L.append("")
    for r in results:
        if not r["examples"]:
            continue
        L.append(f"{r['name']}判定变了的方格举例（按点 → 按门，步行米数）：")
        for via, pd, gd in r["examples"]:
            L.append(f"- 到「{via}」：{pd} → {gd}")
        L.append("")
    L.append("两个方向都有：")
    L.append("")
    L.append(
        "- **缺 → 够得着**：学校的门开在居民这一侧，坐标点却在校园深处。"
        "按点测把居民算成绕到校园另一边。"
    )
    L.append(
        "- **够得着 → 缺**：坐标点被吸附到离它最近的那条路上，那条路贴着围墙、并没有门。"
        "按点测把「隔着围墙」当成「走得到」。"
    )
    L.append("")
    L.append(
        "所以按门测不是放宽标准，而是把终点放到居民真正要走到的地方。"
        "入口取不到的学校仍按坐标点测；入口相距不到 20 米的并成一个，每校最多 4 个。"
    )
    L.append("")
    L.append(
        "两种口径共测 "
        + "、".join(
            f"{r['name']} {r['pairs_total']} 个点对（本次运行实发 {r['pairs']} 个）"
            for r in results
        )
        + "，其余命中缓存。2026-10-04 首次运行实发曹杨 82 个、桃浦 5 个。"
    )
    L.append("")
    return "\n".join(L)


def main() -> int:
    parser = argparse.ArgumentParser(description="小学按校门测距的对照实验")
    parser.add_argument("--dry-run", action="store_true", help="只数要实发多少点对")
    parser.add_argument("--write", action="store_true", help="写入 reports/school-gates.md")
    parser.add_argument("--budget", type=int, default=300, help="每个样例的实发点对上限")
    parser.add_argument("--sites", default="caoyang,taopu")
    args = parser.parse_args()
    results = [
        asyncio.run(run(k.strip(), args.dry_run, args.budget)) for k in args.sites.split(",")
    ]
    if args.dry_run:
        return 0
    text = render(results)
    if args.write:
        REPORT.write_text(text, encoding="utf-8")
        print(f"已写入 {REPORT.relative_to(ROOT)}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    try:
        with single_instance("probe_facility_entries"):
            sys.exit(main())
    except AlreadyRunning as exc:
        raise SystemExit(str(exc)) from exc
