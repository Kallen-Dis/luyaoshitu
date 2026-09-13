"""等时圈算法的命令行验证入口。

在接入 Web 界面之前，先用它确认算法在真实路网上能跑出合理结果，
并观察 API 消耗、绕行系数、方向均衡度等关键指标。

用法：
    python scripts/run_isochrone.py --address "上海市普陀区曹杨新村街道"
    python scripts/run_isochrone.py --lat 31.247979 --lng 121.416775 --directions 24
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.baidu.client import BaiduMapClient  # noqa: E402
from app.baidu.errors import BaiduApiError  # noqa: E402
from app.isochrone.algorithm import IsochroneConfig, compute_isochrone  # noqa: E402

# 等时圈结果作为示例数据随仓库分发，让评审方无需消耗 API 配额即可跑通演示
OUT_DIR = ROOT / "data" / "samples"


def render_ascii(iso, width: int = 61) -> str:
    """把等时圈画成字符图，便于在终端上直观判断形状是否合理。

    终端字符高约为宽的两倍，纵向按半比例压缩，否则正圆会被拉成竖椭圆。
    """
    half_w = width // 2
    half_h = half_w // 2
    scale = iso.max_radius_m / half_w if iso.max_radius_m else 1.0
    grid = [[" "] * width for _ in range(half_h * 2 + 1)]

    for ray in iso.rays:
        rad = math.radians(ray.bearing_deg)
        col = half_w + int(round(ray.boundary_m * math.sin(rad) / scale))
        row = half_h - int(round(ray.boundary_m * math.cos(rad) / scale / 2))
        if 0 <= row < len(grid) and 0 <= col < width:
            grid[row][col] = "x" if ray.truncated_by_barrier else "o"
    grid[half_h][half_w] = "+"
    return "\n".join("".join(r).rstrip() for r in grid)


async def main() -> None:
    parser = argparse.ArgumentParser(description="计算基于真实路网的步行等时圈")
    parser.add_argument("--address", help="中心点地址，将通过地理编码解析")
    parser.add_argument("--lat", type=float, help="中心点纬度（BD09）")
    parser.add_argument("--lng", type=float, help="中心点经度（BD09）")
    parser.add_argument("--minutes", type=float, default=15.0, help="时间阈值，默认 15 分钟")
    parser.add_argument("--directions", type=int, default=36, help="射线方向数，默认 36")
    parser.add_argument("--name", help="样例显示名（可含中文），缺省时由地址或坐标推导")
    parser.add_argument("--id", dest="sample_id", help="样例标识，须为 ASCII，用作文件名与接口路径")
    args = parser.parse_args()

    if args.address is None and (args.lat is None or args.lng is None):
        parser.error("请提供 --address，或同时提供 --lat 与 --lng")

    cfg = IsochroneConfig(minutes=args.minutes, directions=args.directions)
    started = time.perf_counter()

    async with BaiduMapClient() as client:
        if args.address:
            center = await client.geocode(args.address)
            print(f"中心点：{args.address} -> {center[0]:.6f}, {center[1]:.6f}")
        else:
            center = (args.lat, args.lng)
            print(f"中心点：{center[0]:.6f}, {center[1]:.6f}")

        try:
            iso = await compute_isochrone(client, center, cfg)
        except BaiduApiError as exc:
            raise SystemExit(f"计算失败：{exc}")

    elapsed = time.perf_counter() - started
    batches = -(-iso.sampled_points // 100)

    print(f"\n采样 {iso.sampled_points} 个点，分 {batches} 批提交，失败 {iso.failed_points} 个")
    print(f"耗时 {elapsed:.1f} 秒")
    print(f"\n{args.minutes:.0f} 分钟步行等时圈：")
    print(f"  面积       {iso.area_m2/1e6:.3f} 平方公里")
    print(f"  平均半径   {iso.mean_radius_m:.0f} 米")
    print(f"  最短方向   {iso.min_radius_m:.0f} 米")
    print(f"  最远方向   {iso.max_radius_m:.0f} 米")
    print(f"  紧凑度     {iso.compactness}（1 为各方向完全均衡）")

    barriers = [r for r in iso.rays if r.truncated_by_barrier]
    saturated = [r for r in iso.rays if r.saturated]
    if barriers:
        dirs = "、".join(f"{r.bearing_deg:.0f}°" for r in barriers[:8])
        print(f"  障碍截断   {len(barriers)} 个方向（{dirs}{'…' if len(barriers) > 8 else ''}）")
    if saturated:
        print(f"  采样饱和   {len(saturated)} 个方向在 1400 米内未超时，真实边界可能更远")

    ratios = [r.detour_ratio for r in iso.rays if r.detour_ratio]
    if ratios:
        print(f"  绕行系数   平均 {sum(ratios)/len(ratios):.2f}，最大 {max(ratios):.2f}")
        print("             （真实路网距离 ÷ 直线距离，这正是直线缓冲区靠不住的原因）")

    print("\n形状预览（+ 中心，o 边界，x 障碍截断）：")
    print(render_ascii(iso))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    display_name = args.name or args.address or f"{center[0]:.4f}, {center[1]:.4f}"
    # 文件名与接口路径一律用 ASCII 标识，中文名只作为展示属性，
    # 避免 URL 里出现百分号编码的中文，也规避跨平台文件名编码问题
    slug = args.sample_id or f"{center[0]:.4f}_{center[1]:.4f}".replace(".", "")
    slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in slug).strip("-").lower()
    out = OUT_DIR / f"isochrone-{slug}-{args.minutes:.0f}min.geojson"

    payload = iso.to_geojson()
    payload["properties"]["center"] = {"lat": center[0], "lng": center[1]}
    payload["properties"]["name"] = display_name
    payload["properties"]["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nGeoJSON 已写入 {out}")


if __name__ == "__main__":
    asyncio.run(main())
