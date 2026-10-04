"""由体检结果生成规划处方。零 API 消耗，只读已有的覆盖统计与盲区网格。

命题要的是「智能体检与规划助手」和「自动诊疗」。停在「缺什么」只完成了体检；
诊疗必须回答「在哪补、补什么、为什么不是再画一个圆」。

有网格判定和设施坐标时，处方直接读灰色区域的**逐格成因**（regions.classify_missing）：

1. **路网阻隔 → 打通。** 直线 1 公里内就有同类设施、步行却超过 1 公里。按「被哪一家挡在外面」
   把这些格子分组，最大的一组写成处方：往哪个方向、直线多远有哪一家、现在步行要多远；
   再按本圈的典型绕行系数估算打通后能消去多少格。这是估算——还没修的路没法用百度实测。
2. **供给缺口 → 补设。** 直线 1 公里内都没有。以所有网格为候选点做最大覆盖贪心选址：
   每次挑「按典型绕行折算后步行 1 公里能覆盖最多缺口格」的一格，每类最多两处。
   估算的覆盖数由「核验选址」（siteplan.py）用真实路网复核。
3. **查询失败的品类不开方。** 数量未知时给选址建议，等于把接口故障写成规划项目。

没有网格或没有设施坐标（旧数据、只做了覆盖统计）时退回原来的规则：
圈外有、圈内无 → 打通；都没有 → 在最大盲区质心补设；圈内有、网格仍盲 → 加密。
"""

from __future__ import annotations

import statistics
from typing import Any

from ..isochrone.geometry import bearing_deg, haversine_m
from ..poi.catalog import KEY_CATEGORIES
from .regions import _compass, classify_missing, region_labels

# 规划建议只对命题点名的三类设施开方，避免把养老、公园也写成「必须新建」。
KEY_NAMES = tuple(c.name for c in KEY_CATEGORIES)

# 典型绕行系数的取值范围。用各方向实测绕行的中位数：太小会高估打通与补设的效果，
# 太大（被一两条铁路拉高）会把所有选址都判成覆盖不了几格
DETOUR_MIN = 1.2
DETOUR_MAX = 1.8
DETOUR_DEFAULT = 1.35

CONNECT_MIN_CELLS = 3
SITE_MIN_GAIN = 3
# 上面两个门槛按 200 米方格定（3 格 = 0.12 km²）。网格加密后要按面积折算：
# 100 米方格里 3 格只有 0.03 km²，一小撮零散格子就会开出一条处方
THRESHOLD_SPACING_M = 200.0


def min_cells(spacing_m: float, cells_at_reference: int) -> int:
    """把「200 米方格下 N 格」折算成当前间距下的格数，面积口径不变。"""
    if spacing_m <= 0:
        return cells_at_reference
    scale = (THRESHOLD_SPACING_M / spacing_m) ** 2
    return max(cells_at_reference, round(cells_at_reference * scale))


SITE_MAX_PER_CATEGORY = 2
MAX_ITEMS = 8


def typical_detour(properties: dict[str, Any]) -> float:
    """本圈的典型绕行系数：各方向实测绕行的中位数，夹在 [1.2, 1.8]。"""
    values = [float(r["detour"]) for r in properties.get("rays") or [] if r.get("detour")]
    if values:
        d = statistics.median(values)
    elif properties.get("mean_detour"):
        d = float(properties["mean_detour"])
    else:
        d = DETOUR_DEFAULT
    return max(DETOUR_MIN, min(DETOUR_MAX, d))


def _cluster(cells: list[dict[str, Any]], spacing_m: float) -> list[list[dict[str, Any]]]:
    """按间距把盲区网格收成连通块。质心就是建议选址，块越大越该优先处理。"""
    if not cells:
        return []
    n = len(cells)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    thresh = spacing_m * 1.6
    for i in range(n):
        for j in range(i + 1, n):
            dist = haversine_m(cells[i]["lat"], cells[i]["lng"], cells[j]["lat"], cells[j]["lng"])
            if dist <= thresh:
                parent[find(i)] = find(j)

    groups: dict[int, list[dict[str, Any]]] = {}
    for i, cell in enumerate(cells):
        groups.setdefault(find(i), []).append(cell)
    return sorted(groups.values(), key=len, reverse=True)


def _centroid(cells: list[dict[str, Any]]) -> tuple[float, float]:
    return (
        sum(c["lat"] for c in cells) / len(cells),
        sum(c["lng"] for c in cells) / len(cells),
    )


def _item(
    action: str,
    title: str,
    reason: str,
    *,
    category: str | None = None,
    lat: float | None = None,
    lng: float | None = None,
    covers: int = 0,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "action": action,
        "title": title,
        "reason": reason,
        "category": category,
        "lat": round(lat, 6) if lat is not None else None,
        "lng": round(lng, 6) if lng is not None else None,
        "covers": covers,
        **extra,
    }


def _where(ids: list[str], fallback: str) -> str:
    """「灰色区域 A、B 」这样的称呼。编号是字母，后面接汉字时留一个空格。"""
    if not ids:
        return fallback
    return "灰色区域 " + "、".join(ids[:3]) + ("等" if len(ids) > 3 else " ")


# ---------- 按灰色区域成因开方 ----------


def _connect_item(
    name: str,
    cells: list[dict[str, Any]],
    causes: list[dict[str, dict[str, Any]]],
    labels: dict[int, str | None],
    detour: float,
    limit: float,
    min_members: int = CONNECT_MIN_CELLS,
) -> dict[str, Any] | None:
    """路网阻隔：按「被哪一家挡在外面」分组，取最大的一组写成打通处方。

    门槛看两样，满足其一即可：这一组本身够大；或者这一组所在的灰色区域里，
    这一类的阻隔格合起来够大。后者是因为一片连着的阻隔区常被几所学校分着挡：
    曹杨 A 片 15 格缺小学，11 格离朝春中心小学最近、其余离另外几所近，
    只看单组会把一整片连通的阻隔区当成零散格子漏掉。
    """
    groups: dict[tuple[str, float, float], list[int]] = {}
    per_region: dict[str, int] = {}
    for i, entry in enumerate(causes):
        info = entry.get(name)
        if not info or info["cause"] != "barrier":
            continue
        p = info["place"]
        key = (str(p.get("name")), float(p["lat"]), float(p["lng"]))
        groups.setdefault(key, []).append(i)
        if labels.get(i):
            per_region[labels[i]] = per_region.get(labels[i], 0) + 1
    if not groups:
        return None
    (pname, plat, plng), members = max(groups.items(), key=lambda kv: len(kv[1]))
    regional = max((per_region.get(labels[i], 0) for i in members if labels.get(i)), default=0)
    if len(members) < min_members and regional < min_members:
        return None

    closest = min(members, key=lambda i: causes[i][name]["straight_m"])
    straight = causes[closest][name]["straight_m"]
    walk = (cells[closest].get("nearest_m") or {}).get(name)
    gain = sum(1 for i in members if causes[i][name]["straight_m"] * detour <= limit)
    if gain == 0:
        # 这些格子离那一家直线都在 1000/绕行 米开外：就算打通，按本圈的绕行也进不了 1 公里，
        # 开一条「打通」处方没有意义
        return None
    clat = sum(float(cells[i]["lat"]) for i in members) / len(members)
    clng = sum(float(cells[i]["lng"]) for i in members) / len(members)
    direction = _compass(bearing_deg(clat, clng, plat, plng))
    ids = sorted({labels[i] for i in members if labels.get(i)})
    where = _where(ids, "零散盲区")
    # 打通点取最近一格与目标设施的中点：阻隔就在这两者之间
    mid_lat = (float(cells[closest]["lat"]) + plat) / 2
    mid_lng = (float(cells[closest]["lng"]) + plng) / 2
    walk_text = f"，步行最近的{name}却要 {round(walk)} 米" if walk else "，步行却超过 1 公里"
    return _item(
        "connect",
        f"打通往{direction}的步行连接，让{where}走得到「{pname}」",
        f"{where}有 {len(members)} 格缺{name}、直线 1 公里内最近的就是「{pname}」"
        f"（最近一格直线 {round(straight)} 米{walk_text}），缺口在路网而不在数量。"
        f"按本圈典型绕行 {detour:.2f} 倍估算，打通后约 {gain} 格能进入步行 1 公里。"
        "优先核查两者之间的铁路、河道、围墙与缺失的过街设施。",
        category=name,
        lat=mid_lat,
        lng=mid_lng,
        covers=gain,
        basis="estimate",
        cells=len(members),
        region=ids[0] if ids else None,
        direction=direction,
        target={"name": pname, "lat": round(plat, 6), "lng": round(plng, 6)},
    )


def rank_sites(
    cells: list[dict[str, Any]],
    demand: set[int],
    radius_m: float,
) -> list[tuple[int, set[int]]]:
    """每个网格作为候选点时能覆盖的缺口格（直线 ≤ radius_m），按覆盖数从多到少排序。

    覆盖数相同时，圈内格多的优先，再按下标保证结果稳定。
    """
    if not demand:
        return []
    ranked: list[tuple[int, set[int]]] = []
    for j, cand in enumerate(cells):
        clat, clng = float(cand["lat"]), float(cand["lng"])
        covered = {
            i
            for i in demand
            if haversine_m(clat, clng, float(cells[i]["lat"]), float(cells[i]["lng"])) <= radius_m
        }
        if covered:
            ranked.append((j, covered))

    def key(item: tuple[int, set[int]]) -> tuple[int, int, int]:
        j, covered = item
        inside = sum(1 for i in covered if cells[i].get("in_circle", True))
        return (-len(covered), -inside, j)

    ranked.sort(key=key)
    return ranked


def supply_demand(causes: list[dict[str, dict[str, Any]]], name: str) -> set[int]:
    return {i for i, e in enumerate(causes) if (e.get(name) or {}).get("cause") == "supply"}


def greedy_sites(
    cells: list[dict[str, Any]],
    demand: set[int],
    radius_m: float,
    max_sites: int = SITE_MAX_PER_CATEGORY,
    min_gain: int = SITE_MIN_GAIN,
) -> list[tuple[int, set[int]]]:
    """最大覆盖贪心：每一步挑覆盖「剩余」缺口格最多的候选点。"""
    remaining = set(demand)
    ranked = rank_sites(cells, demand, radius_m)
    picks: list[tuple[int, set[int]]] = []
    for _ in range(max_sites):
        best = max(
            ((j, covered & remaining) for j, covered in ranked),
            key=lambda x: (len(x[1]), -x[0]),
            default=None,
        )
        if best is None or len(best[1]) < min_gain:
            break
        picks.append(best)
        remaining -= best[1]
    return picks


def _site_items(
    name: str,
    cells: list[dict[str, Any]],
    causes: list[dict[str, dict[str, Any]]],
    labels: dict[int, str | None],
    detour: float,
    limit: float,
    min_gain: int = SITE_MIN_GAIN,
) -> list[dict[str, Any]]:
    demand = supply_demand(causes, name)
    radius = limit / detour
    out = []
    sites = greedy_sites(cells, demand, radius, min_gain=min_gain)
    for k, (j, covered) in enumerate(sites, start=1):
        ids = sorted({labels[i] for i in covered if labels.get(i)})
        where = _where(ids, "缺口方格")
        inside = sum(1 for i in covered if cells[i].get("in_circle", True))
        # 现版网格只铺在圈内，「其中圈内几格」就是全部，不再重复写
        within = f"（其中圈内 {inside} 格）" if inside < len(covered) else ""
        out.append(
            _item(
                "site",
                f"在{where}补设「{name}」" + (f"（第 {k} 处）" if k > 1 else ""),
                f"网格里有 {len(demand)} 格直线 1 公里内都没有{name}，属于供给缺口，修路也到不了。"
                f"按最大覆盖贪心选址：按本圈典型绕行 {detour:.2f} 倍折算，此处步行 1 公里"
                f"约能覆盖 {len(covered)} 个缺口格{within}。"
                "估算未经路网核验，点「核验选址」用真实路网复核并比较备选点位。",
                category=name,
                lat=float(cells[j]["lat"]),
                lng=float(cells[j]["lng"]),
                covers=len(covered),
                basis="estimate",
                region=ids[0] if ids else None,
                rank=k,
            )
        )
    return out


def _grid_prescriptions(
    properties: dict[str, Any],
    coverage: dict[str, Any],
    blindspots: dict[str, Any],
    failed: set[str],
) -> list[dict[str, Any]]:
    cells = list(blindspots.get("cells") or [])
    limit = float(blindspots.get("walk_limit_m") or 1000.0)
    spacing = float(blindspots.get("grid_spacing_m") or THRESHOLD_SPACING_M)
    _, causes = classify_missing(cells, coverage, limit)
    labels = region_labels(blindspots)
    detour = typical_detour(properties)
    primary: list[dict[str, Any]] = []
    extra: list[dict[str, Any]] = []
    for name in KEY_NAMES:
        if name in failed:
            continue
        connect = _connect_item(
            name, cells, causes, labels, detour, limit, min_cells(spacing, CONNECT_MIN_CELLS)
        )
        if connect:
            primary.append(connect)
        sites = _site_items(
            name, cells, causes, labels, detour, limit, min_cells(spacing, SITE_MIN_GAIN)
        )
        primary += sites[:1]
        extra += sites[1:]
    # 每类的「打通」与第一处「补设」优先，第二处补设排后面，
    # 避免某一类的几处选址把另一类的打通处方挤出列表；同一档内按预计效果排序
    primary.sort(key=lambda p: -p["covers"])
    extra.sort(key=lambda p: -p["covers"])
    return primary + extra


# ---------- 旧规则（没有网格或没有设施坐标时） ----------


def _legacy_prescriptions(
    coverage: dict[str, Any] | None,
    blindspots: dict[str, Any] | None,
    blinds: list[str],
    failed_set: set[str],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    nearby = (coverage or {}).get("nearby_categories") or {}
    spacing = float((blindspots or {}).get("grid_spacing_m") or 150.0)
    cells = list((blindspots or {}).get("cells") or [])

    for name in KEY_NAMES:
        if name in failed_set:
            continue
        if name in blinds:
            near = int(nearby.get(name) or 0)
            missing_cells = [c for c in cells if name in (c.get("missing") or [])]
            clusters = _cluster(missing_cells, spacing)
            lat = lng = None
            covers = len(missing_cells)
            if clusters:
                lat, lng = _centroid(clusters[0])
                covers = len(clusters[0])
            if near > 0:
                out.append(
                    _item(
                        "connect",
                        f"打通步行通道，使圈外 {near} 处「{name}」进得了 15 分钟圈",
                        "检索半径内已经有设施，真实等时圈内为零。"
                        "再按直线距离选址会重复建设；缺口在路网，不在数量。",
                        category=name,
                        lat=lat,
                        lng=lng,
                        covers=covers,
                    )
                )
            else:
                out.append(
                    _item(
                        "site",
                        f"在最大盲区质心补设「{name}」",
                        "圈内与附近均未检索到该类设施，属于供给缺口。"
                        "选址取盲区连通块质心，使尽可能多的走不到的居民点落入 1 公里步行范围。",
                        category=name,
                        lat=lat,
                        lng=lng,
                        covers=covers,
                    )
                )
            continue

        ratio = ((blindspots or {}).get("blind_ratio") or {}).get(name)
        if ratio is None or ratio < 0.15:
            continue
        missing_cells = [c for c in cells if name in (c.get("missing") or [])]
        clusters = _cluster(missing_cells, spacing)
        if not clusters:
            continue
        lat, lng = _centroid(clusters[0])
        out.append(
            _item(
                "densify",
                f"在「{name}」最大盲区加密一处，覆盖约 {len(clusters[0])} 个走不到的居民点",
                f"{ratio:.0%} 的已判定网格步行 1 公里内到不了该类设施，"
                "但圈内并非完全没有——缺的是被屏障切开的局部，适合小点位补点而不是撤并。",
                category=name,
                lat=lat,
                lng=lng,
                covers=len(clusters[0]),
            )
        )
    return out


def prescribe(
    properties: dict[str, Any],
    coverage: dict[str, Any] | None,
    blindspots: dict[str, Any] | None,
    blinds: list[str],
    failed: list[str],
) -> list[dict[str, Any]]:
    """生成处方：路网切割在前，其余按预计效果（覆盖格数）排序，最多 MAX_ITEMS 条。"""
    out: list[dict[str, Any]] = []
    failed_set = set(failed)
    compactness = float(properties.get("compactness") or 1.0)

    if compactness < 0.55:
        out.append(
            _item(
                "network",
                "优先打通路网切割，而不是在直线意义上继续堆设施",
                f"紧凑度仅 {compactness:.2f}，最短方向半径远小于最远方向，"
                "说明铁路、河道或封闭地块在切割可达范围。先补过街或开口，"
                "圈外已有的设施才进得来。",
            )
        )

    has_grid = bool((blindspots or {}).get("cells"))
    has_places = bool((coverage or {}).get("places"))
    if has_grid and has_places:
        out += _grid_prescriptions(properties, coverage or {}, blindspots or {}, failed_set)
        limit = MAX_ITEMS
    else:
        out += _legacy_prescriptions(coverage, blindspots, blinds, failed_set)
        limit = 4

    # 全部正常时也给一条可执行的维持建议，避免报告在「优/良」时变成空白
    if not out:
        blind = sum(1 for c in (blindspots or {}).get("cells") or [] if c.get("missing"))
        reason = (
            f"还有 {blind} 格盲区，但都是零散的小块，没有达到开方的面积门槛。"
            "可以在地图上逐格核实，属实的用共享标注记下来。"
            if blind
            else "圈内品类齐全，网格判定没有盲区。"
        )
        out.append(
            _item(
                "maintain",
                "关键设施步行覆盖基本均衡，维持现有布点并监测路网变化",
                reason + "后续应在施工围挡或路口改造后复检等时圈，避免路网变化把覆盖冲掉。",
            )
        )
    return out[:limit]
