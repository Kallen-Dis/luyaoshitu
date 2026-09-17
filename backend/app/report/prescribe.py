"""由体检结果生成规划处方。零 API 消耗，只读已有的覆盖统计与盲区网格。

命题要的是「智能体检与规划助手」和「自动诊疗」。停在「缺什么」只完成了体检；
诊疗必须回答「在哪补、补什么、为什么不是再画一个圆」。

三条原则：

1. **圈外有、圈内无 → 优先打通，而不是再盖一家。** 桃浦镇检索半径内有设施，
   真实步行圈内为零，再在直线意义上「附近」重复选址解决不了路网切割。
2. **圈内有、网格仍盲 → 在最大连通盲区的质心加密。** 设施已经存在，缺的是
   被铁路/河道切开的那一侧够得着的点。
3. **查询失败的品类不开方。** 数量未知时给选址建议，等于把接口故障写成规划项目。
"""

from __future__ import annotations

from typing import Any

from ..isochrone.geometry import haversine_m
from ..poi.catalog import KEY_CATEGORIES

# 规划建议只对命题点名的三类设施开方，避免把养老、公园也写成「必须新建」。
KEY_NAMES = tuple(c.name for c in KEY_CATEGORIES)


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
            dist = haversine_m(
                cells[i]["lat"], cells[i]["lng"], cells[j]["lat"], cells[j]["lng"]
            )
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
) -> dict[str, Any]:
    return {
        "action": action,
        "title": title,
        "reason": reason,
        "category": category,
        "lat": round(lat, 6) if lat is not None else None,
        "lng": round(lng, 6) if lng is not None else None,
        "covers": covers,
    }


def prescribe(
    properties: dict[str, Any],
    coverage: dict[str, Any] | None,
    blindspots: dict[str, Any] | None,
    blinds: list[str],
    failed: list[str],
) -> list[dict[str, Any]]:
    """生成最多四条处方，按「先打通、再补设、再加密」排序。"""
    out: list[dict[str, Any]] = []
    failed_set = set(failed)
    nearby = (coverage or {}).get("nearby_categories") or {}
    compactness = float(properties.get("compactness") or 1.0)
    spacing = float((blindspots or {}).get("grid_spacing_m") or 150.0)
    cells = list((blindspots or {}).get("cells") or [])

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

    # 全部正常时也给一条可执行的维持建议，避免报告在「优/良」时变成空白
    if not out:
        out.append(
            _item(
                "maintain",
                "关键设施步行覆盖基本均衡，维持现有布点并监测路网变化",
                "圈内品类齐全，网格盲区未形成需要立即补点的连通块。"
                "后续应在施工围挡或路口改造后复检等时圈，避免路网变化把覆盖冲掉。",
            )
        )
    return out[:4]
