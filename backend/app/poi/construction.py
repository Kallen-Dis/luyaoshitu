"""工地 POI 候选：用「工地 / 施工」关键词检索周边，筛出可能立着围挡的地点，交给用户确认。

这是施工围挡识别的第二条线索，和复测巡检互补：

- 复测巡检看「路线变了没有」，需要有旧路线可比；
- 工地检索看「附近登记了哪些工地」，第一次分析的地点也能用。

它的局限同样要说清：地点检索有日配额（申请后每天 3000 次，免费额度只有约 150 次），所以只按需触发、
每个关键词只取一页（共 2 次检索），结果缓存 30 天；POI 是静态登记数据，
召回有限、也可能滞后——查不到不等于没有工地，查到了也不等于围挡挡了路。
所以只给候选，确认后才作为围挡参与计算。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from ..baidu.client import BaiduMapClient
from ..isochrone.geometry import bearing_deg, distance_to_polyline_m, haversine_m

KEYWORDS: tuple[str, ...] = ("工地", "施工")

# 名称里带这些词，基本就是一处施工现场
STRONG_WORDS: tuple[str, ...] = ("工地", "项目部", "在建", "施工现场", "施工区", "施工围挡")
# 带这些词的是卖材料、招工、装修的店和公司，不是现场，一律不要
HARD_DROP: tuple[str, ...] = (
    "招聘",
    "培训",
    "劳务",
    "建材",
    "租赁",
    "五金",
    "装修",
    "装饰",
    "家装",
    "门窗",
    "设计",
)
# 只有「施工」二字时更容易是公司名，再多拦一批
WEAK_DROP: tuple[str, ...] = HARD_DROP + (
    "公司",
    "店",
    "队",
    "中心",
    "事务所",
    "协会",
    "商行",
    "经营部",
    "服务部",
    "工作室",
    "设备",
    "机械",
    "材料",
)

# 候选点离某个方向的步行路线不超过这个距离，就标为「在路线上」，优先确认
ON_ROUTE_M = 60.0
# 名称相同、相距不到这个距离的记录视为同一处
DEDUP_M = 50.0
DEFAULT_RADIUS_M = 50.0

_BRACKETS = re.compile(r"[（(][^）)]*[）)]")


def classify_name(name: str) -> str | None:
    """名称筛选：strong（明确是工地）/ weak（只有「施工」二字、且不像公司）/ None（丢弃）。"""
    if not name or any(w in name for w in HARD_DROP):
        return None
    if any(w in name for w in STRONG_WORDS):
        return "strong"
    if "施工" in name and not any(w in name for w in WEAK_DROP):
        return "weak"
    return None


def _route_lines(feature: dict[str, Any] | None) -> list[tuple[float, list[tuple[float, float]]]]:
    rays = ((feature or {}).get("properties") or {}).get("rays") or []
    out = []
    for r in rays:
        path = r.get("route_path") or []
        if len(path) >= 2:
            out.append((float(r.get("bearing") or 0.0), [(float(p[1]), float(p[0])) for p in path]))
    return out


async def find_construction(
    client: BaiduMapClient,
    center: tuple[float, float],
    radius_m: int = 1500,
    feature: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """检索并筛选工地候选。全部关键词都检索失败时 failed_keywords 等于 KEYWORDS。"""
    before = client.usage_snapshot()
    pages = await asyncio.gather(
        *(
            client.search_poi(kw, center[0], center[1], radius_m, page_size=20, page_num=0)
            for kw in KEYWORDS
        )
    )
    after = client.usage_snapshot()
    lines = _route_lines(feature)

    raw = 0
    dropped = 0
    failed: list[str] = []
    kept: list[dict[str, Any]] = []
    for kw, results in zip(KEYWORDS, pages, strict=True):
        if results is None:
            failed.append(kw)
            continue
        for item in results:
            raw += 1
            name = str(item.get("name") or "").strip()
            loc = item.get("location") or {}
            lat, lng = loc.get("lat"), loc.get("lng")
            strength = classify_name(name)
            if strength is None or lat is None or lng is None:
                dropped += 1
                continue
            lat, lng = float(lat), float(lng)
            dist = haversine_m(center[0], center[1], lat, lng)
            if dist > radius_m * 1.05:
                dropped += 1
                continue
            base = _BRACKETS.sub("", name)
            if any(
                _BRACKETS.sub("", k["name"]) == base
                and haversine_m(k["lat"], k["lng"], lat, lng) <= DEDUP_M
                for k in kept
            ):
                continue
            near = min(
                ((distance_to_polyline_m((lat, lng), pts), b) for b, pts in lines),
                default=None,
                key=lambda x: x[0],
            )
            kept.append(
                {
                    "name": name,
                    "address": str(item.get("address") or ""),
                    "lat": round(lat, 6),
                    "lng": round(lng, 6),
                    "keyword": kw,
                    "strength": strength,
                    "distance_m": round(dist),
                    "bearing": round(bearing_deg(center[0], center[1], lat, lng), 1),
                    "near_route_m": round(near[0]) if near else None,
                    "on_route_bearing": near[1] if near and near[0] <= ON_ROUTE_M else None,
                    "on_route": bool(near and near[0] <= ON_ROUTE_M),
                    "radius_m": DEFAULT_RADIUS_M,
                }
            )

    kept.sort(key=lambda k: (not k["on_route"], k["strength"] != "strong", k["distance_m"]))
    return {
        "candidates": kept,
        "keywords": list(KEYWORDS),
        "failed_keywords": failed,
        "raw_count": raw,
        "dropped": dropped,
        "radius_m": radius_m,
        "routes_compared": len(lines),
        "poi_queries": after["requests"].get("poi", 0) - before["requests"].get("poi", 0),
        "note": (
            "按「工地」「施工」各检索一页，按名称筛掉建材店、装修公司、劳务招聘等；"
            f"离某个方向的步行路线不超过 {ON_ROUTE_M:.0f} 米的排在前面。"
            "POI 是静态登记数据，召回有限：查不到不等于没有工地，查到也不等于挡了路，需人工确认。"
        ),
    }
