"""设施的入口：有面积的设施（学校）从哪里进得去。

地点检索给的设施坐标只是一个点，学校往往标在校园中间。批量算路把终点吸附到离这个点最近的路上，
那条路可能贴着围墙、根本没有门：测出来的距离既可能偏远（住在门这一侧的居民被算成绕到别处），
也可能偏近（吸附到没有门的围墙外）。居民真正要走到的是校门。

入口从两处来，都是地点检索加 scope=2 才给的字段：

- `detail_info.navi_location`：导航点，百度导航实际引到的位置，通常是正门。按品类检索时就带着，
  不多花一次请求；
- `detail_info.children`：子点，学校的「东南 1 门」「北门」和停车场等。按品类检索的结果里没有，
  要按校名再查一次（每所学校 1 次地点检索，缓存 30 天）。

入口 = 分类为「出入口」的子点（停车场的车辆出入口不算）+ 导航点，彼此相距不到 20 米的并成一个。
一个入口都没有时退回设施坐标本身，和以前的做法一样。

2026-10-04 在曹杨实测（scripts/probe_facility_entries.py）：导航点离坐标点 13~102 米；按入口测距后，
缺小学的方格由 26 格变为 18 格，也有 2 格由够得着变为缺——坐标点吸附到的那条路一侧并没有门。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from ..isochrone.geometry import haversine_m

if TYPE_CHECKING:
    from ..baidu.client import BaiduMapClient
    from .collect import Poi

# 相距这么近的两个入口当成同一个：导航点常常就落在某个门上，没必要测两遍
MERGE_M = 20.0
# 每处设施最多测几个入口。点对数随入口数成倍增长，大学城那种十几个门的设施只取最前面几个
MAX_ENTRIES = 4
# 入口终点的缓存量化粒度（米）。默认的 50 米会把相距二三十米的两个门并成一个缓存键
ENTRY_GRID_M = 10.0
# 按校名查校门：在设施坐标多大范围内找回同一条记录、最多查几所（按离中心由近到远）
GATE_SEARCH_RADIUS_M = 300
SAME_RECORD_M = 30.0
MAX_GATE_LOOKUPS = 30

Entry = tuple[float, float, str]


def _point(raw: Any) -> tuple[float, float] | None:
    if not isinstance(raw, Mapping):
        return None
    try:
        lat, lng = float(raw["lat"]), float(raw["lng"])
    except (KeyError, TypeError, ValueError):
        return None
    if lat == 0.0 or lng == 0.0:
        return None
    return lat, lng


def _is_gate(child: Mapping[str, Any]) -> bool:
    tag = str(child.get("classified_poi_tag") or child.get("tag") or "")
    # 「出入口;门」「出入口;东门」算；「出入口;停车场出入口」是车走的，不算
    return tag.startswith("出入口") and "停车" not in tag


def gate_name(child: Mapping[str, Any]) -> str:
    """子点名称形如「上海市朝春中心小学-东南1门」，取连字符后面那段；show_name 实测会错位，不用。"""
    name = str(child.get("name") or "")
    return name.rsplit("-", 1)[-1] if "-" in name else name or "门"


def facility_entries(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """从一条 scope=2 的检索结果里取入口：[{"lat", "lng", "name"}]，最多 MAX_ENTRIES 个。

    没有导航点也没有出入口子点时返回空列表，调用方应退回设施坐标。
    """
    info = record.get("detail_info") or {}
    found: list[dict[str, Any]] = []

    def add(pt: tuple[float, float] | None, name: str) -> None:
        if pt is None or len(found) >= MAX_ENTRIES:
            return
        for e in found:
            if haversine_m(e["lat"], e["lng"], pt[0], pt[1]) < MERGE_M:
                return
        found.append({"lat": round(pt[0], 6), "lng": round(pt[1], 6), "name": name})

    # 子点里有名字的门优先，导航点和某个门重合时保留门的名字
    for child in info.get("children") or []:
        if isinstance(child, Mapping) and _is_gate(child):
            add(_point(child.get("location")), gate_name(child))
    add(_point(info.get("navi_location")), "导航点")
    return found


def as_tuples(entries: Sequence[Mapping[str, Any]]) -> tuple[Entry, ...]:
    return tuple((float(e["lat"]), float(e["lng"]), str(e.get("name") or "")) for e in entries)


def entry_points(place: Any) -> list[tuple[float, float]]:
    """设施的入口坐标。Poi 对象与结果里的 places 字典都认；没有入口时是空列表。"""
    raw = place.get("entries") if isinstance(place, Mapping) else getattr(place, "entries", ())
    out = []
    for e in raw or ():
        if isinstance(e, Mapping):
            out.append((float(e["lat"]), float(e["lng"])))
        else:
            out.append((float(e[0]), float(e[1])))
    return out


def _coords(place: Any) -> tuple[float, float]:
    if isinstance(place, Mapping):
        return float(place["lat"]), float(place["lng"])
    return float(place.lat), float(place.lng)


def destinations(place: Any) -> list[tuple[float, float]]:
    """测距用的终点：有入口就只测入口，没有就测设施坐标。

    不和坐标点取最小值：坐标点在校园里面，它吸附到的那条路可能贴着围墙、根本没有门。
    """
    return entry_points(place) or [_coords(place)]


def straight_m(lat: float, lng: float, place: Any) -> float:
    """到设施的直线距离：有入口按最近的入口算。直线是步行距离的下界，剪枝和成因诊断都用它。"""
    return min(haversine_m(lat, lng, d[0], d[1]) for d in destinations(place))


async def lookup_gates(
    client: BaiduMapClient,
    pois: list[Poi],
    center: tuple[float, float],
    limit: int = MAX_GATE_LOOKUPS,
    concurrent: bool = True,
) -> tuple[list[Poi], int]:
    """按校名再查一次，拿到校门子点，并进各设施的入口。返回 (新的设施列表, 查了几所)。

    查不到（检索失败、没找回同一条记录、没有门）就保留原来的入口（导航点或空）：
    入口只是把测距做得更准，拿不到不能让这一类变成「查询失败」。
    """
    order = sorted(
        range(len(pois)),
        key=lambda i: haversine_m(center[0], center[1], pois[i].lat, pois[i].lng),
    )[:limit]

    async def one(i: int) -> Poi:
        p = pois[i]
        page = await client.search_poi(
            p.raw_name or p.name, p.lat, p.lng, GATE_SEARCH_RADIUS_M, page_size=10, scope=2
        )
        rec = next(
            (
                r
                for r in page or []
                if (pt := _point(r.get("location"))) is not None
                and haversine_m(p.lat, p.lng, pt[0], pt[1]) < SAME_RECORD_M
            ),
            None,
        )
        entries = facility_entries(rec) if rec is not None else []
        return replace(p, entries=as_tuples(entries)) if entries else p

    if concurrent:
        updated = await asyncio.gather(*(one(i) for i in order))
    else:
        updated = [await one(i) for i in order]
    out = list(pois)
    for i, p in zip(order, updated, strict=True):
        out[i] = p
    return out, len(order)
