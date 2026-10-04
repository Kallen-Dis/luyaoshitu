"""对比"直线画圆"与"真实路网等时圈"两种评估口径的差异，生成测试报告。

只读已生成的快照与本地 POI 缓存，不发任何网络请求。

对比三个层面，越往后越能说明问题：

1. **可达面积**：圆的面积 πr²（r = 15 分钟 × 1.2 米/秒 = 1080 米）对比等时圈实测面积。
2. **圈内设施数**：直线 1080 米内的设施数，对比真实等时圈内的设施数。
   前者是现行评估口径会写进报告的数字，后者才是居民真正走得到的。
3. **盲区漏判**：直线法认为"1 公里内有设施即算覆盖"，而真实路网测距会发现
   其中一部分居民点根本走不到。这一项直接暴露直线法的假阴性。

第 3 项能算准，靠的是一条恒等关系：直线距离是步行距离的下界，故
"直线 1 公里内有设施"正是直线法的判定结论，与快照里的实测判定逐格可比。

用法：
    python scripts/compare_straight_line.py
    python scripts/compare_straight_line.py --write   # 同时写入 reports/
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
from app.isochrone.geometry import point_in_polygon  # noqa: E402
from app.poi.catalog import CATEGORIES, KEY_CATEGORIES  # noqa: E402
from app.poi.collect import collect_coverage  # noqa: E402
from app.report.blindspot import (  # noqa: E402
    BlindspotConfig,
    CellResult,
    rank_candidates,
    scope_to_circle,
)
from app.report.score import WALK_SPEED_M_PER_S  # noqa: E402

SAMPLES = ROOT / "data" / "samples"
REPORT = ROOT / "reports" / "straight-line-comparison.md"


def circle_radius_m(minutes: float) -> float:
    return minutes * 60.0 * WALK_SPEED_M_PER_S


async def analyse(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    props = payload["properties"]
    center = (props["center"]["lat"], props["center"]["lng"])
    polygon = [(lat, lng) for lng, lat in payload["geometry"]["coordinates"][0]]
    minutes = props["minutes"]
    radius = circle_radius_m(minutes)

    async with BaiduMapClient() as client:
        coverage = await collect_coverage(client, center, int(props["coverage"]["radius_m"]))

    from app.isochrone.geometry import haversine_m

    facilities = []
    for category in CATEGORIES:
        pois = coverage.pois_of(category.name)
        in_circle = sum(
            1 for p in pois if haversine_m(center[0], center[1], p.lat, p.lng) <= radius
        )
        in_iso = sum(1 for p in pois if point_in_polygon(p.lat, p.lng, polygon))
        facilities.append((category.name, in_circle, in_iso))

    # 逐格比对：直线法说"覆盖"，实测说"走不到"
    cfg = BlindspotConfig()
    # 只比 15 分钟圈内的方格（旧版 1.5 公里网格的快照也先截到圈内）
    blind = scope_to_circle(props.get("blindspots")) or {}
    misjudged = []
    for category in KEY_CATEGORIES:
        pois = coverage.pois_of(category.name)
        straight_ok = truly_blind = unknown = 0
        for cell in blind.get("cells", []):
            probe = CellResult(lat=cell["lat"], lng=cell["lng"])
            # 直线法看的是设施坐标点（传统做法就是这样），不用校门
            if not rank_candidates(probe, pois, cfg.walk_limit_m, entrances=False):
                continue  # 直线法也判为盲区，两种口径一致
            straight_ok += 1
            if category.name in cell.get("unknown", []):
                unknown += 1
            elif category.name in cell.get("missing", []):
                truly_blind += 1
        misjudged.append((category.name, straight_ok, truly_blind, unknown))

    circle_area = 3.14159265 * radius * radius / 1e6
    return {
        "name": props["name"],
        "minutes": minutes,
        "radius_m": radius,
        "circle_area": circle_area,
        "iso_area": props["area_m2"] / 1e6,
        "detour_mean": props.get("mean_detour"),
        "detour_max": props.get("max_detour"),
        "facilities": facilities,
        "misjudged": misjudged,
        "cell_count": blind.get("cell_count", 0),
        "spacing_m": blind.get("grid_spacing_m"),
    }


def render(results: list[dict]) -> str:
    first = results[0]
    lines = [
        "# 直线缓冲区 vs 真实路网等时圈：实测对比",
        "",
        "> 本报告由 `scripts/compare_straight_line.py` 依据 `data/samples/` 的快照与",
        "> 本地 POI 缓存离线生成，不消耗任何 API 配额。",
        "",
        f"对照口径取现行评估最常用的做法：以中心点画半径 {first['radius_m']:.0f} 米的圆",
        f"（{first['minutes']:.0f} 分钟 × {WALK_SPEED_M_PER_S} 米/秒），圈内设施即算"
        "居民可获得的服务。",
        "",
        "## 一、可达面积",
        "",
        "| 样例社区 | 直线画圆 | 真实路网 | 真实/直线 | 平均绕行 | 最大绕行 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        ratio = r["iso_area"] / r["circle_area"]
        lines.append(
            f"| {r['name']} | {r['circle_area']:.3f} km² | {r['iso_area']:.3f} km² "
            f"| {ratio:.0%} | {r['detour_mean']} | {r['detour_max']} |"
        )

    lines += [
        "",
        "面积是最容易被高估的一项，但它只是起点——真正影响结论的是设施数。",
        "",
        "## 二、圈内民生设施数",
        "",
    ]
    for r in results:
        lines += [
            f"### {r['name']}",
            "",
            "| 品类 | 直线圆内 | 等时圈内 | 高估 |",
            "| --- | --- | --- | --- |",
        ]
        total_c = total_i = 0
        for name, in_circle, in_iso in r["facilities"]:
            total_c += in_circle
            total_i += in_iso
            gap = in_circle - in_iso
            lines.append(f"| {name} | {in_circle} | {in_iso} | {'+' if gap else ''}{gap} |")
        lines.append(f"| **合计** | **{total_c}** | **{total_i}** | **+{total_c - total_i}** |")
        lines.append("")

    lines += [
        "## 三、盲区漏判（直线法的假阴性）",
        "",
        "直线法的判定规则是「直线 1 公里内有此类设施即算覆盖」。",
        "下表只统计直线法判为**覆盖**的居民点，看其中有多少实测走不到。",
        "居民点取 15 分钟步行圈内的网格中心。",
        "",
    ]
    for r in results:
        grid = f"{r['spacing_m']:.0f} 米方格" if r.get("spacing_m") else "网格"
        lines += [
            f"### {r['name']}（15 分钟圈内 {r['cell_count']} 个 {grid}）",
            "",
            "| 关键设施 | 直线法判为覆盖 | 实测走不到 | 漏判率 | 未判定 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for name, straight_ok, truly_blind, unknown in r["misjudged"]:
            judged = straight_ok - unknown
            rate = f"{truly_blind / judged:.1%}" if judged else "—"
            lines.append(f"| {name} | {straight_ok} | {truly_blind} | {rate} | {unknown} |")
        lines.append("")

    lines += [
        "「未判定」是测距失败的网格，不计入漏判率——把接口失败算成盲区，",
        "等于让 API 故障伪装成民生问题，这是本项目明确拒绝的做法。",
        "",
        "## 结论",
        "",
    ]

    # 按比例而非绝对差值挑最极端的样例：设施基数小的社区差值小，但结论错得更彻底
    def retained(r: dict) -> float:
        circle = sum(c for _, c, _ in r["facilities"])
        return (sum(i for _, _, i in r["facilities"]) / circle) if circle else 1.0

    worst = min(results, key=retained)
    w_circle = sum(c for _, c, _ in worst["facilities"])
    w_iso = sum(i for _, _, i in worst["facilities"])
    missing_all = w_iso == 0 and w_circle > 0

    lines.append(
        f"最极端的是{worst['name']}：直线法会报告圈内有 **{w_circle} 处**民生设施，"
        f"而真实路网 {worst['minutes']:.0f} 分钟内能走到的是 **{w_iso} 处**。"
    )
    if missing_all:
        lines += [
            "两种口径给出的不是「精确度差异」，而是完全相反的结论——",
            "一个说配套尚可，一个说六个品类在真实可达范围内全部缺失。",
        ]
    else:
        lines.append(f"按此口径，现行评估会把可获得的服务高估 {w_circle / max(w_iso, 1):.1f} 倍。")

    # 从实测数据里挑漏判最严重的一项，避免把结论写死
    worst_cell = None
    for r in results:
        for name, straight_ok, blind, unknown in r["misjudged"]:
            judged = straight_ok - unknown
            if judged and (worst_cell is None or blind / judged > worst_cell[2]):
                worst_cell = (r["name"], name, blind / judged)

    if worst_cell:
        lines += [
            "",
            f"而第三张表暴露的是另一类偏差：{worst_cell[0]}的「{worst_cell[1]}」，"
            f"直线法判为覆盖的居民点里有 **{worst_cell[2]:.1%}** 实测走不到。",
            "面积口径的偏差是均匀的，可达性的偏差却集中在被铁路、河道切开的局部——",
            "恰恰是最需要被看见的地方。这也是本项目要逐网格判定、而非只报一个总分的原因。",
            "",
        ]
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description="直线圆与等时圈的对比测试")
    parser.add_argument("--write", action="store_true", help="写入 reports/ 目录")
    args = parser.parse_args()

    paths = sorted(SAMPLES.glob("isochrone-*.geojson"))
    if not paths:
        raise SystemExit("未找到样例快照，请先运行 scripts/run_isochrone.py")

    results = [await analyse(p) for p in paths]
    text = render(results)
    print(text)
    if args.write:
        REPORT.write_text(text + "\n", encoding="utf-8")
        print(f"\n已写入 {REPORT}")


if __name__ == "__main__":
    asyncio.run(main())
