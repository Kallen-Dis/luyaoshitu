"""离线核查快照的圈内计数，并估算盲区判定的配额消耗。

只读本地缓存与已生成的快照，不发任何网络请求，可放心反复运行。

两个存在的理由：

1. 桃浦镇的快照出现"六个品类圈内计数全为 0、检索半径内却有 7~12 处"，
   这既可能是真实情况（工业区外围配套稀疏），也可能是点在多边形判定出了错。
   用最近设施的实际距离与等时圈半径对照，才能把两者区分开。
2. 批量算路的日配额按**点对数**计量，重跑前需要先知道要花多少。
   直线距离是步行距离的下界，故候选集可以离线算准，消耗区间也就能离线估出来。

用法：
    python scripts/check_coverage_offline.py taopu
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.baidu.client import BaiduMapClient  # noqa: E402
from app.isochrone.geometry import grid_points, haversine_m, point_in_polygon  # noqa: E402
from app.poi.catalog import CATEGORIES, KEY_CATEGORIES  # noqa: E402
from app.poi.collect import collect_coverage  # noqa: E402
from app.report.blindspot import BlindspotConfig, CellResult, rank_candidates  # noqa: E402

SAMPLES = ROOT / "data" / "samples"


async def main() -> None:
    parser = argparse.ArgumentParser(description="离线核查圈内设施计数")
    parser.add_argument("sample", help="样例标识，如 taopu")
    parser.add_argument("--minutes", type=int, default=15)
    args = parser.parse_args()

    path = SAMPLES / f"isochrone-{args.sample}-{args.minutes}min.geojson"
    payload = json.loads(path.read_text(encoding="utf-8"))
    props = payload["properties"]
    center = (props["center"]["lat"], props["center"]["lng"])
    # 快照存的是 GeoJSON 的 [经度, 纬度]，内部几何一律用 (纬度, 经度)
    polygon = [(lat, lng) for lng, lat in payload["geometry"]["coordinates"][0]]
    radius = int(props["coverage"]["radius_m"])

    print(f"{props['name']}  中心 {center[0]:.6f},{center[1]:.6f}")
    print(f"等时圈半径 {props['min_radius_m']:.0f}~{props['max_radius_m']:.0f} 米，"
          f"检索半径 {radius} 米")

    # 中心点必须落在自己的等时圈内，否则就是几何判定本身错了
    print(f"中心点在多边形内：{point_in_polygon(center[0], center[1], polygon)}")

    async with BaiduMapClient() as client:
        coverage = await collect_coverage(client, center, radius)

    print(f"\n{'品类':<8}{'附近':>4}{'圈内':>6}   最近三处的直线距离（米）与是否在圈内")
    for category in CATEGORIES:
        pois = coverage.pois_of(category.name)
        if not pois:
            print(f"{category.name:<8}{0:>4}{0:>6}   —")
            continue
        ranked = sorted(
            pois, key=lambda p: haversine_m(center[0], center[1], p.lat, p.lng)
        )
        inside = sum(1 for p in pois if point_in_polygon(p.lat, p.lng, polygon))
        detail = "  ".join(
            f"{haversine_m(center[0], center[1], p.lat, p.lng):.0f}"
            f"{'(圈内)' if point_in_polygon(p.lat, p.lng, polygon) else '(圈外)'}"
            for p in ranked[:3]
        )
        print(f"{category.name:<8}{len(pois):>4}{inside:>6}   {detail}")

    cfg = BlindspotConfig()
    cells = [
        CellResult(lat=lat, lng=lng) for lat, lng in grid_points(polygon, cfg.grid_spacing_m)
    ]
    print(f"\n盲区判定的配额消耗估算（{len(cells)} 个网格，间距 {cfg.grid_spacing_m:.0f} 米）：")

    lower = upper = 0
    free = 0
    for category in KEY_CATEGORIES:
        pois = coverage.pois_of(category.name)
        counts = [len(rank_candidates(c, pois, cfg.walk_limit_m)) for c in cells]
        pruned = sum(1 for n in counts if n == 0)
        # 下界：每个有候选的网格第一轮就命中；上界：候选全测完仍不达标
        lo = len(cells) - pruned
        hi = sum(min(n, cfg.max_rounds) for n in counts)
        lower += lo
        upper += hi
        free += pruned
        print(
            f"  {category.name:<8}直线剪枝掉 {pruned:>3} 个网格（零消耗），"
            f"其余点对 {lo}~{hi} 个"
        )

    print(f"  热力图     {len(cells)} 个点对（中心到各网格）")
    print(f"  合计       {len(cells) + lower}~{len(cells) + upper} 个点对")
    print(f"  剪枝省下   {free} 个网格的测距，直线距离就已超标，无需请求")


if __name__ == "__main__":
    asyncio.run(main())
