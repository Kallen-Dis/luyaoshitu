"""生活圈体检评分。

评分分三层，越往后越依赖地点检索配额，故都能独立缺失：

1. **路网层** —— 只看等时圈本身：真实面积相对直线圆的比例、方向均衡度、绕行系数。
   只消耗批量算路，实时计算立刻出分。
2. **覆盖层** —— 各品类设施在圈内的数量，计数为 0 即该品类盲区。
3. **均衡层** —— 网格级判定的结果：圈内有设施，但有多少比例的居民点步行 1 公里
   内仍到不了。命题点名的菜市场、药店、小学按此逐点判定。

第 2、3 层缺失时总分只按路网层的权重归一，而不是把缺失当零分——
"没测"和"没有"是两件事，混同会让一次配额不足伪装成社区配套差。
"""

from __future__ import annotations

from typing import Any

from .prescribe import prescribe
from .wide_grid import wide_grid

# 常人步行速度。直线距离除以它，是步行时间的下界。
WALK_SPEED_M_PER_S = 1.2

# 覆盖层权重：命题点名菜市场、药店、小学，这三类权重大于其余
CATEGORY_WEIGHTS: dict[str, float] = {
    "生鲜采买": 1.4,
    "医药": 1.3,
    "基础教育": 1.3,
    "基础医疗": 1.1,
    "养老服务": 1.0,
    "文体休闲": 0.9,
}

# 各维度的合成权重。缺失维度不计入，按实际参与项的权重和归一。
WEIGHT_REACH = 0.30
WEIGHT_COMPACT = 0.20
WEIGHT_DETOUR = 0.15
WEIGHT_COVER = 0.20
WEIGHT_EQUITY = 0.15


def ideal_area_km2(minutes: float, speed_m_per_s: float = WALK_SPEED_M_PER_S) -> float:
    radius = minutes * 60.0 * speed_m_per_s
    return (3.141592653589793 * radius * radius) / 1e6


def reach_score(
    area_km2: float, minutes: float, speed_m_per_s: float = WALK_SPEED_M_PER_S
) -> float:
    """真实面积占直线圆的百分比，封顶 100。直线圆半径随出行方式的速度变化。"""
    ideal = ideal_area_km2(minutes, speed_m_per_s)
    if ideal <= 0:
        return 0.0
    return round(min(100.0, 100.0 * area_km2 / ideal), 1)


def compact_score(compactness: float) -> float:
    return round(max(0.0, min(100.0, compactness * 100.0)), 1)


def detour_score(mean_detour: float | None) -> float | None:
    """绕行系数 1.0（完全沿直线）得 100；2.25 得 0。"""
    if mean_detour is None:
        return None
    return round(max(0.0, min(100.0, 100.0 - (mean_detour - 1.0) * 80.0)), 1)


def cover_score(categories: dict[str, int] | None) -> tuple[float | None, list[str]]:
    """按加权覆盖率打分；计数为 0 的品类列入盲区清单。"""
    if not categories:
        return None, []
    blinds = [name for name, count in categories.items() if count == 0]
    total_w = sum(CATEGORY_WEIGHTS.get(name, 1.0) for name in categories)
    got = sum(CATEGORY_WEIGHTS.get(name, 1.0) for name, count in categories.items() if count > 0)
    score = round(100.0 * got / total_w, 1) if total_w else 0.0
    return score, blinds


def equity_score(blind_ratio: dict[str, float] | None) -> float | None:
    """设施可达均衡度：关键品类盲区网格占比的平均值取反。

    圈内有药店不等于人人走得到。这一项衡量的正是"有设施"与"到得了"之间的落差，
    只有网格级判定能给出。
    """
    if not blind_ratio:
        return None
    mean_blind = sum(blind_ratio.values()) / len(blind_ratio)
    return round(max(0.0, min(100.0, 100.0 * (1.0 - mean_blind))), 1)


def overall_score(
    reach: float,
    compact: float,
    detour: float | None,
    cover: float | None,
    equity: float | None = None,
) -> float:
    parts: list[tuple[float, float]] = [
        (reach, WEIGHT_REACH),
        (compact, WEIGHT_COMPACT),
    ]
    if detour is not None:
        parts.append((detour, WEIGHT_DETOUR))
    if cover is not None:
        parts.append((cover, WEIGHT_COVER))
    if equity is not None:
        parts.append((equity, WEIGHT_EQUITY))
    weight_sum = sum(w for _, w in parts)
    return round(sum(s * w for s, w in parts) / weight_sum, 1) if weight_sum else 0.0


def grade(score: float) -> str:
    if score >= 85:
        return "优"
    if score >= 70:
        return "良"
    if score >= 55:
        return "中"
    return "弱"


def build_report(
    properties: dict[str, Any],
    coverage: dict[str, Any] | None = None,
    blindspots: dict[str, Any] | None = None,
    polygon: list[tuple[float, float]] | None = None,
) -> dict[str, Any]:
    """由等时圈属性、可选的覆盖统计与网格盲区判定生成体检报告。"""
    minutes = float(properties.get("minutes") or 15)
    area = float(properties.get("area_km2") or 0)
    compactness = float(properties.get("compactness") or 0)
    speed = float(properties.get("speed_m_per_s") or WALK_SPEED_M_PER_S)
    mean_detour = properties.get("mean_detour")
    if mean_detour is not None:
        mean_detour = float(mean_detour)

    categories = None
    failed_categories: list[str] = []
    if coverage:
        categories = coverage.get("categories")
        failed_categories = list(coverage.get("failed_categories") or [])

    # 有设施坐标时，分数和处方改用地图上那套 200 米方格。
    # 旧的圈内步行格仍留在 properties.blindspots 里，供耗时热力使用，不再拿来打分。
    places = (coverage or {}).get("places") or []
    center = properties.get("center")
    grid = blindspots
    if places and center and polygon and len(polygon) >= 3:
        grid = wide_grid(center, places, polygon)

    blind_ratio = grid.get("blind_ratio") if grid else None

    r = reach_score(area, minutes, speed)
    c = compact_score(compactness)
    d = detour_score(mean_detour)
    cov, blinds = cover_score(categories)
    eq = equity_score(blind_ratio)
    total = overall_score(r, c, d, cov, eq)
    ideal = ideal_area_km2(minutes, speed)
    inflation = round(ideal / area, 2) if area > 0 else None

    return {
        "total": total,
        "grade": grade(total),
        "dimensions": {
            "reach": r,
            "compact": c,
            "detour": d,
            "cover": cov,
            "equity": eq,
        },
        "blinds": blinds,
        "ideal_area_km2": round(ideal, 3),
        "area_ratio": round(area / ideal, 3) if ideal else 0,
        "straight_inflation": inflation,
        "categories": categories,
        # 检索失败的品类：数量未知，前端须显示为「查询失败」而非缺失
        "failed_categories": failed_categories,
        "blind_ratio": blind_ratio,
        "blind_cell_count": grid.get("blind_count") if grid else None,
        "cell_count": grid.get("cell_count") if grid else None,
        "grid_basis": grid.get("basis") if grid else None,
        "coverage_source": coverage.get("source") if coverage else None,
        "coverage_pending": coverage is None,
        "blindspots_pending": grid is None,
        "prescriptions": prescribe(
            properties, coverage, grid, blinds, failed_categories
        ),
    }
