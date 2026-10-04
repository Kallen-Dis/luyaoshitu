"""POI 采集与多源清洗。

采集侧按品类的多个关键词并查、翻页取全量；清洗侧解决多源合并必然带来的四类脏数据：

1. **坐标缺失或非法** —— 少数记录没有 location，或经纬度为 0，直接剔除；
2. **误召回** —— 关键词模糊匹配带回的非目标设施，按品类的 exclude 词表按名称拦；
3. **超范围** —— 百度对 radius 的处理并不严格，偶尔返回圈外更远的点，按实距复核；
4. **重复** —— 同一设施被多个关键词返回，或连锁店在同址重复挂牌。

去重键取「归一化名称 + 量化坐标」而非 uid：uid 在多关键词召回下并不总是一致，
而同名同址几乎可以断定是同一设施。名称归一化会去掉「（凯旋路店）」这类后缀，
故还要叠加坐标，避免把同品牌的两家不同门店误并成一家。

**失败一律以 None 表达，绝不退化为空列表。** 空列表会被下游读成「此地没有这类设施」，
让一次接口抖动伪装成服务盲区——这是本项目最危险的错误。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from ..baidu.client import BaiduMapClient
from ..isochrone.geometry import haversine_m, point_in_polygon
from .catalog import CATEGORIES, Category
from .entries import as_tuples, facility_entries, lookup_gates

# 同名设施视为同一家的坐标量化粒度（米）。与磁盘缓存的 50 米保持一致。
DEDUP_GRID_M = 50.0

_FULLWIDTH_OFFSET = 0xFEE0


@dataclass(frozen=True)
class Poi:
    """一条清洗后的设施记录。

    name/address 仅在进程内与本地缓存中使用。随开源仓库分发的快照只包含派生统计，
    不含这些原始字段——POI 原始记录的分发合规性尚未获得书面答复，按保守口径处理。
    """

    name: str
    lat: float
    lng: float
    category: str
    address: str | None = None
    # baidu：地点检索；user：用户补录（见 markings.apply），marking_id 指回那条标注
    source: str = "baidu"
    marking_id: int | None = None
    # 入口（纬度, 经度, 名称）：有面积的设施按入口测距，见 poi/entries.py；空表示按坐标点测
    entries: tuple[tuple[float, float, str], ...] = ()
    # 百度的分类标签（scope=2 才有），如「教育培训;小学」
    tag: str | None = None
    # 检索返回的原始名称（含「(岚西校区)」这类后缀），按校名查校门时用
    raw_name: str | None = None


@dataclass
class CleanStats:
    """清洗过程的留存与剔除计数，用于在报告中说明数据质量。"""

    raw: int = 0
    invalid: int = 0
    excluded: int = 0
    out_of_range: int = 0
    duplicated: int = 0

    @property
    def dropped(self) -> int:
        return self.invalid + self.excluded + self.out_of_range + self.duplicated

    def as_dict(self) -> dict[str, int]:
        return {
            "raw": self.raw,
            "invalid": self.invalid,
            "excluded": self.excluded,
            "out_of_range": self.out_of_range,
            "duplicated": self.duplicated,
            "dropped": self.dropped,
        }


@dataclass
class CategoryResult:
    category: str
    pois: list[Poi]
    stats: CleanStats
    searched_keywords: int = 0
    # 按校名查校门的次数（地点检索，缓存 30 天）
    gate_lookups: int = 0


@dataclass
class CoverageResult:
    """一次完整的覆盖采集结果。

    failed 里的品类**计数未知**，不是零。序列化时它们不会进入 categories，
    而是单独列出，由前端明确显示为「查询失败」。
    """

    center: tuple[float, float]
    radius_m: int
    results: list[CategoryResult] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    searches: int = 0

    @property
    def categories(self) -> dict[str, int]:
        return {r.category: len(r.pois) for r in self.results}

    def pois_of(self, category: str) -> list[Poi]:
        return next((r.pois for r in self.results if r.category == category), [])

    def as_dict(self, polygon: list[tuple[float, float]] | None = None) -> dict[str, Any]:
        """序列化派生统计。

        给定 polygon 时 categories 为**圈内**计数，另附采集半径内的总数：
        两者的差额本身就是有价值的信息——设施明明在附近，只是走不进这个 15 分钟圈。
        """
        inside = counts_within(self, polygon) if polygon is not None else self.categories
        places = [
            {
                "category": r.category,
                "name": p.name,
                "lat": round(p.lat, 6),
                "lng": round(p.lng, 6),
                "in_circle": point_in_polygon(p.lat, p.lng, polygon) if polygon else True,
                **({"source": p.source, "marking_id": p.marking_id} if p.source != "baidu" else {}),
                **(
                    {
                        "entries": [
                            {"lat": round(lat, 6), "lng": round(lng, 6), "name": name}
                            for lat, lng, name in p.entries
                        ]
                    }
                    if p.entries
                    else {}
                ),
            }
            for r in self.results
            for p in r.pois
        ]
        return {
            "categories": inside,
            "nearby_categories": self.categories,
            "failed_categories": self.failed,
            "searches": self.searches,
            "gate_lookups": sum(r.gate_lookups for r in self.results),
            "radius_m": self.radius_m,
            "clean_stats": {r.category: r.stats.as_dict() for r in self.results},
            # 只随本次响应给地图打点，不写入对外分发的快照文件。
            "places": places,
        }


def normalize_name(raw: str) -> str:
    """名称归一化：全角转半角、去空白、剥掉括号内的分店后缀。

    「曹杨路药房（兰溪路店）」与「曹杨路药房(兰溪路店)」在不同关键词下都可能返回，
    归一化后同为「曹杨路药房」，配合坐标量化即可判为同一家。
    """
    chars = []
    for ch in raw:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            chars.append(chr(code - _FULLWIDTH_OFFSET))
        elif not ch.isspace():  # 全角空格 U+3000 也属 isspace，一并去掉
            chars.append(ch)
    name = "".join(chars)
    for opener, closer in (("(", ")"), ("（", "）"), ("[", "]")):
        start = name.find(opener)
        if start > 0 and name.endswith(closer):
            name = name[:start]
            break
    return name


def _quantize(value: float, grid_m: float) -> int:
    return round(value / (grid_m / 111_320.0))


def clean(
    records: list[dict[str, Any]],
    category: Category,
    center: tuple[float, float],
    radius_m: float,
    polygon: list[tuple[float, float]] | None = None,
) -> tuple[list[Poi], CleanStats]:
    """把原始检索记录清洗成去重后的设施列表。

    polygon 非空时以「落在等时圈内」为准，而不是以圆形半径为准——这正是本项目的
    立论：真实可达范围不是圆。半径只作为检索时的粗筛与坐标复核的兜底。
    """
    stats = CleanStats(raw=len(records))
    seen: set[tuple[str, int, int]] = set()
    out: list[Poi] = []

    for item in records:
        raw_name = str(item.get("name") or "")
        loc = item.get("location") or {}
        lat, lng = loc.get("lat"), loc.get("lng")
        if not raw_name or lat is None or lng is None:
            stats.invalid += 1
            continue
        lat, lng = float(lat), float(lng)
        if lat == 0 or lng == 0:
            stats.invalid += 1
            continue

        name = normalize_name(raw_name)
        if not name:
            stats.invalid += 1
            continue
        if any(word in raw_name for word in category.exclude):
            stats.excluded += 1
            continue
        tag = str((item.get("detail_info") or {}).get("tag") or "") or None
        if tag and category.exclude_tags and set(tag.split(";")) & set(category.exclude_tags):
            stats.excluded += 1
            continue

        if polygon is not None:
            if not point_in_polygon(lat, lng, polygon):
                stats.out_of_range += 1
                continue
        elif haversine_m(center[0], center[1], lat, lng) > radius_m:
            stats.out_of_range += 1
            continue

        key = (name, _quantize(lat, DEDUP_GRID_M), _quantize(lng, DEDUP_GRID_M))
        if key in seen:
            stats.duplicated += 1
            continue
        seen.add(key)
        out.append(
            Poi(
                name=name,
                lat=lat,
                lng=lng,
                category=category.name,
                address=str(item.get("address") or "") or None,
                entries=as_tuples(facility_entries(item)) if category.entrances else (),
                tag=tag,
                raw_name=raw_name,
            )
        )

    return out, stats


def counts_within(coverage: CoverageResult, polygon: list[tuple[float, float]]) -> dict[str, int]:
    """统计落在等时圈内的设施数。

    采集半径必须大于等时圈本身：圈边缘的网格走 900 米就能到圈外的药店，
    那家药店不该算进"圈内覆盖"，但必须参与盲区判定。两个用途因此共用一次采集、
    分开统计——重新按小半径采一遍等于白烧一倍配额。
    """
    return {
        r.category: sum(1 for p in r.pois if point_in_polygon(p.lat, p.lng, polygon))
        for r in coverage.results
    }


async def collect_category(
    client: BaiduMapClient,
    category: Category,
    center: tuple[float, float],
    radius_m: int = 1000,
    polygon: list[tuple[float, float]] | None = None,
) -> CategoryResult | None:
    """采集单个品类。任一关键词查询失败即返回 None（计数未知，不是零）。"""
    # 关键品类要罩住圈内网格（最远约 1.5 公里）再加 1 公里判定阈值，检索半径约 2.5 公里；
    # 每词 3 页（60 条）在曹杨这样的密集街区会被截断，漏掉的设施会凭空造出盲区
    max_pages = 6 if category.key_facility else 3
    # 按入口测距的品类要 scope=2：每条结果多带导航点与分类标签，请求次数不变
    extra: dict[str, Any] = {"scope": 2} if category.entrances else {}
    pages = await asyncio.gather(
        *(
            client.search_poi_all(kw, center[0], center[1], radius_m, max_pages=max_pages, **extra)
            for kw in category.keywords
        )
    )
    if any(p is None for p in pages):
        return None

    merged: list[dict[str, Any]] = []
    for page in pages:
        merged.extend(page or [])
    pois, stats = clean(merged, category, center, radius_m, polygon)
    lookups = 0
    if category.entrances and pois:
        pois, lookups = await lookup_gates(client, pois, center)
    return CategoryResult(
        category=category.name,
        pois=pois,
        stats=stats,
        searched_keywords=len(category.keywords),
        gate_lookups=lookups,
    )


async def collect_coverage(
    client: BaiduMapClient,
    center: tuple[float, float],
    radius_m: int = 1000,
    polygon: list[tuple[float, float]] | None = None,
    categories: tuple[Category, ...] = CATEGORIES,
) -> CoverageResult:
    """并发采集全部品类。单品类失败只影响该品类，其余照常出结果。"""
    gathered = await asyncio.gather(
        *(collect_category(client, c, center, radius_m, polygon) for c in categories)
    )

    coverage = CoverageResult(center=center, radius_m=radius_m)
    for category, result in zip(categories, gathered, strict=True):
        if result is None:
            coverage.failed.append(category.name)
            continue
        coverage.results.append(result)
        coverage.searches += result.searched_keywords
    return coverage
