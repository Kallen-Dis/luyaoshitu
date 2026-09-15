"""离线核查快照的圈内计数是否可信。

只读本地缓存与已生成的快照，不发任何网络请求，可放心反复运行。

存在的理由：桃浦镇的快照出现"六个品类圈内计数全为 0、检索半径内却有 7~12 处"，
这既可能是真实情况（工业区外围配套稀疏），也可能是点在多边形判定出了错。
用最近设施的实际距离与等时圈半径对照，才能把两者区分开。

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
from app.isochrone.geometry import haversine_m, point_in_polygon  # noqa: E402
from app.poi.catalog import CATEGORIES  # noqa: E402
from app.poi.collect import collect_coverage  # noqa: E402

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


if __name__ == "__main__":
    asyncio.run(main())
