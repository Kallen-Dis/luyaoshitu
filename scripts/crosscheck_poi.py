"""用百度地图 Agent Plan 的语义检索给关键设施「第二次召回」，核对两份样例的盲区结论，写成报告。

应用里的「AI 二次核对」按钮做的是同一件事（backend/app/poi/crosscheck.py），这个脚本把两份样例
逐片问一遍、按补录做路网实测，写成 reports/poi-crosscheck.md。提问、比对、缓存都走同一个模块：
同样的问题在页面和脚本里只问一次（回答缓存在 .cache/agent_plan/）。

对每片灰色区域、每个缺的关键品类，以区域锚点为中心问一次「附近最近的 X」，把返回的设施和快照里
同类设施逐个比对（同名 500 米内或相距 100 米内算同一家）：

- **已收录**：两条通道都找到了，结论有交叉印证；
- **未收录、但直线 1 公里内有缺这一类的方格**：可能是漏召回，盲区或「供给缺口」需要复核；
- **未收录、且离所有缺口方格都超过 1 公里**：不影响结论。

用法：
    python scripts/crosscheck_poi.py                 # 两份样例，打印核对结果
    python scripts/crosscheck_poi.py --write         # 同时写入 reports/poi-crosscheck.md
    python scripts/crosscheck_poi.py --dry-run       # 只列出会问哪些问题，不发请求
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from _singleton import AlreadyRunning, single_instance

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.config import ENV_PATH, PROJECT_ROOT, _load_env_file  # noqa: E402
from app.isochrone.geometry import haversine_m  # noqa: E402
from app.poi.catalog import by_name  # noqa: E402
from app.poi.crosscheck import (  # noqa: E402
    QUESTIONS,
    WALK_LIMIT_M,
    AgentPlanClient,
    Gap,
    crosscheck,
    gaps_of,
    to_review,
)

SAMPLES = ROOT / "data" / "samples"
REPORT = ROOT / "reports" / "poi-crosscheck.md"
# 两份样例都在普陀区；和页面上按逆地理编码得到的 region 一致，缓存互通
REGION = "上海市普陀区"


@dataclass
class Site:
    key: str
    name: str
    gaps: list[Gap]
    feature: dict[str, Any] = field(default_factory=dict)


def load_site(key: str) -> Site:
    feature = json.loads((SAMPLES / f"isochrone-{key}-15min.geojson").read_text(encoding="utf-8"))
    name = feature["properties"]["name"].replace("上海市普陀区", "")
    return Site(key=key, name=name, gaps=gaps_of(feature), feature=feature)


@dataclass
class Usage:
    requests: int = 0
    cache_hits: int = 0


async def ask_all(sites: list[Site], token: str | None) -> tuple[dict, Usage]:
    """逐份样例、逐个问题核对。单个问题失败只记在那一行，不会被当成「没有」。"""
    results: dict[str, tuple[Site, list[dict[str, Any]]]] = {}
    async with AgentPlanClient(token, PROJECT_ROOT / ".cache", pause_s=1.0) as ap:
        for site in sites:
            result = await crosscheck(site.feature, ap, REGION, limit=None)
            if result.aborted:
                print(f"[{site.name}] Agent Plan 中途停下：{result.aborted}", file=sys.stderr)
            results[site.key] = (site, result.rows)
        return results, Usage(ap.requests, ap.cache_hits)


def verify_on_network(
    results: dict[str, tuple[Site, list[dict[str, Any]]]], budget: int
) -> dict[str, int]:
    """把要复核的设施当成「补录」，用模拟新建做一次路网实测：缺口格到它步行 1 公里内的有几格。

    点对数 = 缺这一类、且直线 1 公里内的方格数，事先就能算出来；超出预算的不测。
    走项目的批量算路客户端（主 AK、按点对缓存），测过的点对不会再花配额。
    """
    import asyncio

    from app.baidu.client import BaiduMapClient
    from app.report.simulate import simulate_facility

    todo = []
    for site, rows in results.values():
        cells = site.feature["properties"]["blindspots"]["cells"]
        for row in rows:
            for f in to_review(row):
                need = sum(
                    1
                    for c in cells
                    if row["gap"].category in (c.get("missing") or [])
                    and haversine_m(c["lat"], c["lng"], f["lat"], f["lng"]) <= WALK_LIMIT_M
                )
                todo.append((site, row["gap"].category, f, need))

    async def go() -> dict[str, int]:
        used = skipped = 0
        async with BaiduMapClient() as client:
            for site, category, f, need in todo:
                if used + need > budget:
                    f["verify"] = {"status": "over_budget", "candidates": need}
                    skipped += 1
                    continue
                sim = await simulate_facility(client, site.feature, category, f["lat"], f["lng"])
                used += sim["pairs_used"]
                f["verify"] = {
                    "status": sim["basis"],
                    "candidates": sim["candidate_count"],
                    "covered": sim["covered_count"],
                    "pairs": sim["pairs_used"],
                    "before": sim["before"],
                    "after": sim["after"],
                }
        return {"pairs": used, "skipped": skipped, "checked": len(todo) - skipped}

    return asyncio.run(go()) if todo else {"pairs": 0, "skipped": 0, "checked": 0}


def render(
    results: dict[str, tuple[Site, list[dict[str, Any]]]],
    ap: Usage,
    verified: dict[str, int] | None,
) -> str:
    L = ["# 关键设施二次召回：Agent Plan 交叉核对", ""]
    L.append(
        f"> 由 `scripts/crosscheck_poi.py` 生成（{time.strftime('%Y-%m-%d')}）。"
        "页面上的「AI 二次核对」做同一件事，这里把两份样例逐片核对并按补录做路网实测。"
    )
    L.append("")
    L.append(
        "快照里的设施来自地点检索（`place/v2/search`）按关键词召回，漏召回会凭空造出盲区。"
        "这里换一条召回通道：对每片灰色区域、每个缺的关键品类，以区域锚点为中心，"
        "用百度地图 Agent Plan 的语义地点检索问一次「附近最近的 X」（按距离排序，返回前 10 条），"
        "只留这一类的主点（学校的停车场、出入口这类子点，名字带「小学」的托管班都去掉），"
        "再和快照里的同类设施逐个比对：同名或相距 100 米内算同一家。"
    )
    L.append("")
    L.append(
        "- **已收录**：两条通道都找到了，结论有交叉印证。\n"
        "- **未收录、直线 1 公里外**：离所有缺这一类的方格都超过 1 公里，"
        "步行只会更远，不影响结论。\n"
        "- **未收录、直线 1 公里内**：可能改变结论，要复核。"
    )
    L.append("")
    total_review = 0
    for site, rows in results.values():
        L.append(f"## {site.name}")
        L.append("")
        L.append(
            "| 灰色区域 | 缺 | 缺口格（供给 / 阻隔） | Agent Plan 返回同类 | 已收录 | "
            "未收录、1 公里外 | 未收录、1 公里内 |"
        )
        L.append("| --- | --- | --- | --- | --- | --- | --- |")
        for r in rows:
            g: Gap = r["gap"]
            found = r["found"]
            review = to_review(r)
            total_review += len(review)
            matched = sum(1 for f in found if f["matched"])
            L.append(
                f"| {g.region} | {g.category} | {len(g.cells)}（{g.supply} / {g.barrier}） | "
                f"{len(found)} | {matched} | {len(found) - matched - len(review)} | "
                f"**{len(review)}** |"
            )
        L.append("")
        for r in rows:
            g = r["gap"]
            matched = sorted({f["name"] for f in r["found"] if f["matched"]})
            nearest = min((f["nearest_gap_m"] for f in r["found"]), default=None)
            line = f"- {g.region} · {g.category}："
            if r.get("error"):
                L.append(line + f"没核对成功（{r['error']}），不能据此判断有没有漏收录。")
                continue
            line += (
                f"离缺口格最近的同类设施直线 {nearest} 米；"
                if nearest is not None
                else "没有返回同类；"
            )
            line += f"交叉印证 {len(matched)} 家"
            if matched:
                line += f"（{'、'.join(matched[:4])}{'等' if len(matched) > 4 else ''}）"
            line += "。"
            L.append(line)
            for f in sorted(to_review(r), key=lambda x: x["nearest_gap_m"]):
                v = f.get("verify") or {}
                tail = ""
                if v.get("status") == "network":
                    tail = (
                        f"按补录做路网实测：直线 1 公里内缺{g.category}的 {v['candidates']} 格里，"
                        f"步行 1 公里内够得着 **{v['covered']} 格**"
                        f"（{v['candidates']} 个点对，本次实发 {v['pairs']}，其余命中缓存）"
                    )
                    if v["covered"] == 0:
                        tail += "，结论不变"
                    else:
                        tail += (
                            f"，总分 {v['before']['score']} → {v['after']['score']}，"
                            "核实后应补录"
                        )
                elif v.get("status") == "over_budget":
                    tail = f"超出点对预算未实测（需要 {v['candidates']} 个点对）"
                elif v:
                    tail = "批量算路配额已用尽，未实测"
                cat = by_name(g.category)
                words = [w for w in (cat.keywords if cat else ()) if w in f["name"]]
                why = (
                    f"名称里已含检索词「{words[0]}」，不是词表的问题，是地点检索这次没有返回它"
                    if words
                    else "名称里不含现有的检索词，可以考虑补进词表"
                )
                L.append(
                    f"  - 待复核：{f['name']}，离最近的缺口格直线 {f['nearest_gap_m']} 米；{why}"
                    + (f"。{tail}。" if tail else "。")
                )
        L.append("")

    L.append("## 结论")
    L.append("")
    flipped = [
        f
        for _, rows in results.values()
        for r in rows
        for f in to_review(r)
        if (f.get("verify") or {}).get("covered")
    ]
    unverified = [
        f
        for _, rows in results.values()
        for r in rows
        for f in to_review(r)
        if (f.get("verify") or {}).get("status") != "network"
    ]
    if total_review == 0:
        L.append(
            "两条召回通道对缺口格附近的关键设施没有分歧："
            "Agent Plan 找到的同类设施要么已经在快照里，要么离所有缺口格都超过 1 公里。"
            "盲区结论不是关键词漏召回造成的。"
        )
    elif not flipped and not unverified:
        L.append(
            f"Agent Plan 找到 {total_review} 处快照里没有、且离缺口格直线不到 1 公里的设施，"
            "按补录做了路网实测，没有一格因此变成「够得着」：盲区结论不变。"
            "这些设施仍值得补进来（这一次检索确实漏了），核实后用「补录设施」标注；"
            "叫法不在检索词里的，再补进 `backend/app/poi/catalog.py`。"
        )
    else:
        L.append(
            f"有 {total_review} 处设施需要复核"
            + (f"，其中 {len(flipped)} 处实测会消去缺口格" if flipped else "")
            + "。核实是真实对外的设施后，用「补录设施」标注加进来重算，"
            "或者把它的叫法补进 `backend/app/poi/catalog.py` 的关键词。"
        )
    L.append("")
    thin = [
        f"{site.name} {r['gap'].region}（{r.get('returned', 0)} 条里 {len(r['found'])} 条）"
        for site, rows in results.values()
        for r in rows
        if r.get("returned") and len(r["found"]) <= 3
    ]
    L.append("**局限**：")
    L.append(
        "- 每个缺口只问一次、以区域锚点为中心、取前 10 条。大片区域（桃浦 A 覆盖整个圈）"
        "边缘方格附近的设施，可能排不进前 10。"
    )
    if thin:
        L.append(
            "- 学校的停车场、出入口这些子点也占前 10 条的名额，过滤后剩下的主点不多："
            + "、".join(thin)
            + "。"
        )
    L.append(
        "- 「菜市场、农贸市场或生鲜超市」这个问法，Agent Plan 实际只返回了市场类，"
        "没有返回生鲜超市；生鲜超市的召回这里核对不到。"
    )
    L.append("- Agent Plan 返回的是地图登记信息，查到不等于对外开放、正在营业。")
    L.append("")
    asked = ap.requests + ap.cache_hits
    cost = (
        f"共 {asked} 个问题，回答缓存在 `.cache/agent_plan/`（同样的问题不会再问）；"
        f"本次运行 Agent Plan 实发 {ap.requests} 次。地点检索配额消耗 0"
    )
    if verified:
        cost += (
            f"；路网实测 {verified['checked']} 处，本次批量算路实发 {verified['pairs']} 个点对"
            "（命中缓存的不计）"
        )
        if verified["skipped"]:
            cost += f"（{verified['skipped']} 处超出预算未测）"
    else:
        cost += "；批量算路 0（未做路网实测，加 `--verify` 才测）"
    L.append(cost + "。")
    L.append("")
    return "\n".join(L)


def main() -> int:
    parser = argparse.ArgumentParser(description="用 Agent Plan 交叉核对关键设施召回")
    parser.add_argument("--write", action="store_true", help="写入 reports/poi-crosscheck.md")
    parser.add_argument("--dry-run", action="store_true", help="只列出要问的问题")
    parser.add_argument("--sites", default="taopu,caoyang", help="逗号分隔的样例 id")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="对要复核的设施按补录做路网实测（花批量算路点对，主 AK）",
    )
    parser.add_argument("--budget", type=int, default=200, help="--verify 的点对上限，默认 200")
    args = parser.parse_args()

    _load_env_file(ENV_PATH)
    sites = [load_site(key.strip()) for key in args.sites.split(",")]
    if args.dry_run:
        for site in sites:
            for g in site.gaps:
                print(
                    f"[{site.name}] {g.region} 缺{g.category} {len(g.cells)} 格："
                    f"{QUESTIONS[g.category]}  @ {g.anchor[0]:.6f},{g.anchor[1]:.6f}"
                )
        return 0

    token = os.environ.get("BAIDU_MAP_AUTH_TOKEN", "").strip() or None
    results, usage = asyncio.run(ask_all(sites, token))
    verified = verify_on_network(results, args.budget) if args.verify else None
    text = render(results, usage, verified)
    if args.write:
        REPORT.write_text(text, encoding="utf-8")
        print(f"已写入 {REPORT.relative_to(ROOT)}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    try:
        with single_instance("crosscheck_poi"):
            sys.exit(main())
    except AlreadyRunning as exc:
        raise SystemExit(str(exc)) from exc
