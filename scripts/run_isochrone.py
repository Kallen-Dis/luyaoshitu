"""等时圈与服务盲区的命令行入口，同时用于生成随仓库分发的样例快照。

在接入 Web 界面之前，先用它确认算法在真实路网上能跑出合理结果，
并观察 API 消耗、绕行系数、方向均衡度等关键指标。

用法：
    python scripts/run_isochrone.py --address "上海市普陀区曹杨新村街道"
    python scripts/run_isochrone.py --lat 31.247979 --lng 121.416775 --directions 24
    python scripts/run_isochrone.py --lat 31.284817 --lng 121.369523 --id taopu

默认连带做设施覆盖采集与网格盲区判定；只想验证等时圈形状时加 --no-coverage，
可完全不碰地点检索配额。
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
from app.poi.collect import collect_coverage  # noqa: E402
from app.report.blindspot import BlindspotConfig, identify_blindspots  # noqa: E402
from app.report.score import build_report  # noqa: E402

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
    parser.add_argument(
        "--no-coverage",
        action="store_true",
        help="只算等时圈，跳过设施采集与盲区判定（不消耗地点检索配额）",
    )
    parser.add_argument(
        "--grid-spacing",
        type=float,
        default=150.0,
        help="盲区判定的网格间距（米），默认 150",
    )
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
            raise SystemExit(f"计算失败：{exc}") from exc

        coverage = None
        blind = None
        blind_cfg = BlindspotConfig(grid_spacing_m=args.grid_spacing)
        if not args.no_coverage:
            # 采集半径覆盖整个等时圈再加 1 公里判定阈值：圈外的设施对圈边居民依然有效
            radius = int(iso.max_radius_m + blind_cfg.walk_limit_m)
            print(f"\n采集民生设施，检索半径 {radius} 米…")
            try:
                coverage = await collect_coverage(client, center, radius)
                blind = await identify_blindspots(
                    client, center, iso.polygon, coverage, blind_cfg
                )
            except BaiduApiError as exc:
                raise SystemExit(f"设施采集失败：{exc}") from exc

        matrix_failures = client.matrix_failures
        matrix_pairs = client.matrix_pairs

    elapsed = time.perf_counter() - started
    batches = -(-iso.sampled_points // 100)

    print(f"\n采样 {iso.sampled_points} 个点，分 {batches} 批提交，失败 {iso.failed_points} 个")
    print(f"耗时 {elapsed:.1f} 秒")
    # 批量算路的日配额按点对数计量，故这里报点对数而非请求数
    print(f"本次实发算路点对 {matrix_pairs} 个（命中缓存的不计入配额）")
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

    if coverage is not None:
        inside = coverage.as_dict(iso.polygon)
        print("\n圈内民生设施（括号内为检索半径内的总数）：")
        for name, count in inside["categories"].items():
            nearby = inside["nearby_categories"][name]
            stats = inside["clean_stats"][name]
            flag = "  <- 圈内缺失" if count == 0 else ""
            print(
                f"  {name:<6} {count:>3} 处（附近 {nearby}）"
                f"  原始 {stats['raw']} 条，剔除 {stats['dropped']} 条{flag}"
            )
        if coverage.failed:
            print(f"  检索失败品类：{'、'.join(coverage.failed)}（数量未知，未计入评分）")
        print(f"  共发起 {coverage.searches} 次地点检索（命中缓存不计入配额）")

    if blind is not None:
        print(f"\n网格盲区判定（间距 {blind.config.grid_spacing_m:.0f} 米）：")
        print(f"  网格总数   {len(blind.cells)}")
        print(f"  盲区网格   {len(blind.blind_cells)}")
        for name, ratio in blind.blind_ratio.items():
            print(f"  {name:<6} {ratio*100:>5.1f}% 的居民点步行 1 公里内到不了")
        unknown = sum(1 for c in blind.cells if c.unknown)
        if unknown:
            print(f"  测距失败   {unknown} 个网格未判定（不计入占比，也不算盲区）")
        if matrix_failures:
            print(
                f"  丢块       {len(matrix_failures)} 次矩阵请求整块失败，"
                f"是上面「测距失败」的主因："
            )
            for line in matrix_failures:
                print(f"               {line}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    display_name = args.name or args.address or f"{center[0]:.4f}, {center[1]:.4f}"
    # 文件名与接口路径一律用 ASCII 标识，中文名只作为展示属性，
    # 避免 URL 里出现百分号编码的中文，也规避跨平台文件名编码问题
    slug = args.sample_id or f"{center[0]:.4f}_{center[1]:.4f}".replace(".", "")
    slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in slug).strip("-").lower()
    out = OUT_DIR / f"isochrone-{slug}-{args.minutes:.0f}min.geojson"

    payload = iso.to_geojson()
    props = payload["properties"]
    props["center"] = {"lat": center[0], "lng": center[1]}
    props["name"] = display_name
    props["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    # 快照只写派生结果：品类计数、清洗统计、网格判定。POI 的店名与地址不入库、不分发。
    if coverage is not None:
        cov = coverage.as_dict(iso.polygon)
        cov["source"] = f"预生成快照，检索半径 {cov['radius_m']} 米"
        props["coverage"] = cov
    if blind is not None:
        props["blindspots"] = blind.as_dict()
    props["report"] = build_report(props, props.get("coverage"), props.get("blindspots"))

    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nGeoJSON 已写入 {out}")
    report = props["report"]
    print(f"体检总分 {report['total']}（{report['grade']}）")


if __name__ == "__main__":
    asyncio.run(main())
