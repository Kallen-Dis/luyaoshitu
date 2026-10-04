"""等时圈与服务盲区的命令行入口，同时用于生成随仓库分发的样例快照。

在接入 Web 界面之前，先用它确认算法在真实路网上能跑出合理结果，
并观察 API 消耗、绕行系数、方向均衡度、过街等待等关键指标。

用法：
    python scripts/run_isochrone.py --address "上海市普陀区曹杨新村街道"
    python scripts/run_isochrone.py --lat 31.247979 --lng 121.416775 --directions 24
    python scripts/run_isochrone.py --lat 31.284817 --lng 121.369523 --id taopu
    python scripts/run_isochrone.py --lat 31.284817 --lng 121.369523 --id taopu-closure \\
        --closure 31.2860,121.3710,60

默认连带做设施覆盖采集与网格盲区判定（15 分钟圈内 100 米方格，逐格路网实测；
设施检索半径 2.5 公里，圈边上的居民走到圈外的设施也算有），
并用每方向一条步行路线补回过街等待。只想验证等时圈形状时加 --no-coverage，
可完全不碰地点检索配额；--no-delay 关闭过街等待校正（不调步行路线规划）。

每个环节实发多少点对、多少请求记在快照的 properties.api_usage 里（命中缓存的不计），
docs/api-optimization.md 的实测数据以它为准。
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
from app.isochrone.refine import RefineOptions, closures_from  # noqa: E402
from app.poi.collect import collect_coverage  # noqa: E402
from app.report.blindspot import BlindspotConfig, identify_blindspots  # noqa: E402
from app.report.score import build_report  # noqa: E402
from app.travel import get_mode  # noqa: E402

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
            mark = "#" if ray.truncated_by_closure else ("x" if ray.truncated_by_barrier else "o")
            grid[row][col] = mark
    grid[half_h][half_w] = "+"
    return "\n".join("".join(r).rstrip() for r in grid)


def _usage(client: BaiduMapClient) -> tuple[int, dict[str, int], float]:
    return client.matrix_pairs, dict(client.request_counts), time.perf_counter()


def _diff(before: tuple, after: tuple) -> dict:
    pairs0, req0, t0 = before
    pairs1, req1, t1 = after
    requests = {k: v - req0.get(k, 0) for k, v in req1.items() if v - req0.get(k, 0)}
    return {"matrix_pairs": pairs1 - pairs0, "requests": requests, "elapsed_s": round(t1 - t0, 1)}


STAGE_NAMES = {
    "isochrone": "等时圈（含过街校正）",
    "coverage": "设施采集",
    "blindspots": "网格盲区",
}


def parse_closure(text: str) -> dict:
    parts = [p.strip() for p in text.split(",")]
    if len(parts) not in (2, 3):
        raise argparse.ArgumentTypeError("围挡格式为 纬度,经度[,半径米]，如 31.2860,121.3710,60")
    try:
        lat, lng = float(parts[0]), float(parts[1])
        radius = float(parts[2]) if len(parts) == 3 else 50.0
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"围挡坐标不是数字：{text}") from exc
    return {"lat": lat, "lng": lng, "radius_m": radius}


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
        "--no-delay",
        action="store_true",
        help="不做过街等待校正（不调用步行路线规划）",
    )
    parser.add_argument(
        "--closure",
        action="append",
        type=parse_closure,
        default=[],
        help="施工围挡，格式 纬度,经度[,半径米]，可重复给出",
    )
    parser.add_argument(
        "--grid-spacing",
        type=float,
        default=100.0,
        help="盲区判定的网格间距（米，只在 15 分钟圈内布点），默认 100",
    )
    parser.add_argument(
        "--grid-extent",
        type=float,
        default=1500.0,
        help="设施检索至少罩住的半径（米，再加 1 公里判定阈值），默认 1500",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="配额中途用完时仍覆盖原快照（默认另存为 .partial.geojson）",
    )
    parser.add_argument(
        "--mode",
        default="walk",
        help="出行方式：walk / ride / drive / drive_traffic，默认 walk",
    )
    args = parser.parse_args()

    if args.address is None and (args.lat is None or args.lng is None):
        parser.error("请提供 --address，或同时提供 --lat 与 --lng")

    try:
        mode = get_mode(args.mode)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    cfg = IsochroneConfig(minutes=args.minutes, directions=args.directions, mode_id=mode.id)
    closures = closures_from(args.closure)
    refine = RefineOptions(delay=not args.no_delay, closures=closures)
    started = time.perf_counter()

    async with BaiduMapClient() as client:
        if args.address:
            center = await client.geocode(args.address)
            print(f"中心点：{args.address} -> {center[0]:.6f}, {center[1]:.6f}")
        else:
            center = (args.lat, args.lng)
            print(f"中心点：{center[0]:.6f}, {center[1]:.6f}")

        # 分环节记下实发量：调用前后取差（客户端的计数是累计值）
        stages: dict[str, dict] = {}
        mark = _usage(client)

        def close_stage(name: str) -> None:
            nonlocal mark
            now = _usage(client)
            stages[name] = _diff(mark, now)
            mark = now

        try:
            iso = await compute_isochrone(client, center, cfg, refine)
        except BaiduApiError as exc:
            raise SystemExit(f"计算失败：{exc}") from exc
        close_stage("isochrone")

        coverage = None
        blind = None
        blind_cfg = BlindspotConfig(grid_spacing_m=args.grid_spacing, extent_m=args.grid_extent)
        if not args.no_coverage:
            # 检索半径罩住网格范围再加 1 公里判定阈值：圈边上的居民走到圈外的设施也算有
            radius = int(max(iso.max_radius_m, blind_cfg.extent_m) + blind_cfg.walk_limit_m)
            print(f"\n采集民生设施，检索半径 {radius} 米…")
            try:
                coverage = await collect_coverage(client, center, radius)
                close_stage("coverage")
                if mode.allow_grid_blindspots:
                    blind = await identify_blindspots(
                        client,
                        center,
                        iso.polygon,
                        coverage,
                        blind_cfg,
                        refine=iso.refine,
                        closures=closures,
                    )
                    close_stage("blindspots")
            except BaiduApiError as exc:
                raise SystemExit(f"设施采集失败：{exc}") from exc

        matrix_failures = client.matrix_failures
        matrix_pairs = client.matrix_pairs
        route_calls = client.request_counts.get("route", 0)
        route_failures = client.route_failures

    elapsed = time.perf_counter() - started
    batches = -(-iso.sampled_points // 100)

    print(f"\n采样 {iso.sampled_points} 个点，分 {batches} 批提交，失败 {iso.failed_points} 个")
    print(f"耗时 {elapsed:.1f} 秒")
    # 批量算路的日配额按点对数计量，故这里报点对数而非请求数
    print(f"本次实发算路点对 {matrix_pairs} 个（命中缓存的不计入配额）")
    print(f"本次实发步行路线规划 {route_calls} 次，失败 {len(route_failures)} 次")
    for name, stage in stages.items():
        reqs = "，".join(f"{k} {v} 次" for k, v in stage["requests"].items()) or "无请求"
        print(
            f"  {STAGE_NAMES.get(name, name):<12} 点对 {stage['matrix_pairs']:>5}  "
            f"请求：{reqs}  用时 {stage['elapsed_s']} 秒"
        )
    print(f"\n{args.minutes:.0f} 分钟{mode.label}等时圈：")
    print(f"  面积       {iso.area_m2/1e6:.3f} 平方公里")
    print(f"  平均半径   {iso.mean_radius_m:.0f} 米")
    print(f"  最短方向   {iso.min_radius_m:.0f} 米")
    print(f"  最远方向   {iso.max_radius_m:.0f} 米")
    print(f"  紧凑度     {iso.compactness}（1 为各方向完全均衡）")

    if iso.refine is not None:
        info = iso.refine.as_dict()
        print(
            f"  过街校正   {info['routes_ok']}/{info['routes_requested']} 个方向取到路线，"
            f"每公里多出约 {info['delay_per_km_s']} 秒，"
            f"边界内共过街 {info['boundary_crossings']} 次"
        )
        print(f"             校正前 {info['raw_area_km2']} km² → 校正后 {info['area_km2']} km²")
        if closures:
            print(f"  施工围挡   {len(closures)} 处，截断 {info['closure_rays']} 个方向")

    barriers = [r for r in iso.rays if r.truncated_by_barrier and not r.truncated_by_closure]
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

    print("\n形状预览（+ 中心，o 边界，x 障碍截断，# 围挡截断）：")
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
        print(f"  共检索 {coverage.searches} 个关键词（命中缓存不计入配额）")

    if blind is not None:
        scope = (
            f"中心 {blind.config.extent_m:.0f} 米内"
            if blind.config.layout == "disc"
            else "15 分钟圈内"
        )
        print(
            f"\n网格盲区判定（{scope}、间距 {blind.config.grid_spacing_m:.0f} 米，逐格路网实测）："
        )
        in_circle = sum(c.in_circle for c in blind.cells)
        print(f"  网格总数   {len(blind.cells)}（其中 {in_circle} 格在圈内）")
        print(f"  盲区网格   {len(blind.blind_cells)}")
        for name, ratio in blind.blind_ratio.items():
            print(f"  {name:<6} {ratio*100:>5.1f}% 的居民点步行 1 公里内到不了")
        unknown = sum(1 for c in blind.cells if c.unknown)
        if unknown:
            print(f"  测距失败   {unknown} 个网格未判定（不计入占比，也不算盲区）")
        if blind.closure_check is not None:
            chk = blind.closure_check.as_dict()
            print(
                f"  围挡核验   取路线 {chk['checked_pairs']} 条，挡住 {chk['blocked_pairs']} 条，"
                f"无法核验 {chk['unverified_pairs']} 条，围挡内设施 {chk['excluded_places']} 处"
            )
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
    props["closures"] = [c.as_dict() for c in closures]

    # 快照带设施名称与坐标供地图打点，不含电话与街道地址
    if coverage is not None:
        cov = coverage.as_dict(iso.polygon)
        cov["source"] = f"预生成快照，检索半径 {cov['radius_m']} 米"
        props["coverage"] = cov
    if blind is not None:
        props["blindspots"] = blind.as_dict()
    props["report"] = build_report(props, props.get("coverage"), props.get("blindspots"))
    props["api_usage"] = {
        "note": "本次生成的实发量（命中缓存的不计）；同一中心此前测过的点对会命中缓存",
        "total_matrix_pairs": matrix_pairs,
        "total_requests": dict(client.request_counts),
        "elapsed_s": round(elapsed, 1),
        "stages": stages,
        "settings": {
            "max_qps": client._s.max_qps,
            "matrix_concurrency": client._s.matrix_concurrency,
            "route_concurrency": client._s.route_concurrency,
        },
    }

    # 配额在中途用完时，盲区会有一批「未知」格子。不能拿它覆盖一份完整的快照
    if client.quota_events and not args.allow_partial:
        out = out.with_name(out.stem + ".partial.geojson")
        services = "、".join(e["service"] for e in client.quota_events)
        print(f"\n注意：{services}配额中途用完，结果不完整，另存为 {out.name}，原快照未改动")
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nGeoJSON 已写入 {out}")
    report = props["report"]
    print(f"体检总分 {report['total']}（{report['grade']}）")


if __name__ == "__main__":
    asyncio.run(main())
