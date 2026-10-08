"""生活圈体检评分。

评分分三层，越往后越依赖地点检索配额，故都能独立缺失：

1. **路网层** —— 只看等时圈本身：可达面积、方向均衡度、绕行系数。
   只消耗批量算路，实时计算立刻出分。三项都以**理想方格路网**为满分（见下）。
2. **覆盖层** —— 各品类设施在圈内的数量，计数为 0 即该品类盲区。
3. **均衡层** —— 网格级判定的结果：圈内有设施，但有多少比例的居民点步行 1 公里
   内仍到不了。命题点名的菜市场、药店、小学按此逐点判定。

第 2、3 层缺失时总分只按路网层的权重归一，而不是把缺失当零分——
"没测"和"没有"是两件事，混同会让一次配额不足伪装成社区配套差。

**路网层的满分基准是理想方格路网，不是直线。** 直线（以中心画圆、径直走过去）是任何真实路网
都达不到的上界：拿它当满分时，一个路网完全规整、六类设施全部走得到的社区也只有 80 分，
「优」形同虚设。
方格路网（曼哈顿距离）是规划里常用的参照，同样走 r 米：

- 可达范围是对角线 2r 的菱形，面积 2r²，是直线圆 πr² 的 2/π；
- 最短方向半径（对角方向）÷ 最长方向半径（沿街方向）= 1/√2；
- 各方向的绕行系数 |cos θ| + |sin θ|，平均为 4/π ≈ 1.27。

这三个值各记 100 分，比方格路网还好（有斜路、放射路）的也封顶 100。红绿灯与过街等待照样扣分：
满分假设的是一路畅通，等待是真实的损失。直线圆的对比不丢，仍作为「直线法高估几倍」单列在报告里。
"""

from __future__ import annotations

import math
from typing import Any

from ..poi.fresh import FRESH, eligible
from .prescribe import prescribe
from .regions import gray_regions

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


# 理想方格路网的三个参照值（推导见模块说明）
GRID_COMPACTNESS = 1.0 / math.sqrt(2.0)
GRID_DETOUR = 4.0 / math.pi
# 绕行系数到这里记 0 分：平均要多走 1.25 倍，相当于每个方向都在绕大圈
DETOUR_ZERO = 2.25


def _radius_m(minutes: float, speed_m_per_s: float) -> float:
    return minutes * 60.0 * speed_m_per_s


def ideal_area_km2(minutes: float, speed_m_per_s: float = WALK_SPEED_M_PER_S) -> float:
    """直线画圆的面积：传统缓冲区法的口径，只用来说明直线法高估了几倍，不作评分基准。"""
    radius = _radius_m(minutes, speed_m_per_s)
    return (math.pi * radius * radius) / 1e6


def grid_area_km2(minutes: float, speed_m_per_s: float = WALK_SPEED_M_PER_S) -> float:
    """理想方格路网上同样时间走得到的菱形面积 2r²：路网可达一项的满分基准。"""
    radius = _radius_m(minutes, speed_m_per_s)
    return (2.0 * radius * radius) / 1e6


def reach_score(
    area_km2: float, minutes: float, speed_m_per_s: float = WALK_SPEED_M_PER_S
) -> float:
    """可达面积占理想方格路网菱形的百分比，封顶 100。半径随出行方式的速度变化。"""
    grid = grid_area_km2(minutes, speed_m_per_s)
    if grid <= 0:
        return 0.0
    return round(min(100.0, 100.0 * area_km2 / grid), 1)


def compact_score(compactness: float) -> float:
    """最短 ÷ 最长方向半径，方格路网的 1/√2 记满分。"""
    return round(max(0.0, min(100.0, 100.0 * compactness / GRID_COMPACTNESS)), 1)


def detour_score(mean_detour: float | None) -> float | None:
    """平均绕行系数不超过方格路网的 4/π 得 100，到 2.25 得 0，中间线性。"""
    if mean_detour is None:
        return None
    span = DETOUR_ZERO - GRID_DETOUR
    return round(max(0.0, min(100.0, 100.0 * (DETOUR_ZERO - mean_detour) / span)), 1)


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


def grid_basis(blindspots: dict[str, Any] | None) -> str | None:
    """用一句话说清网格判定的口径，报告、图例与导出共用。"""
    if not blindspots:
        return None
    spacing = float(blindspots.get("grid_spacing_m") or 0)
    limit_km = float(blindspots.get("walk_limit_m") or 1000) / 1000
    if blindspots.get("layout") == "disc":
        extent_km = float(blindspots.get("extent_m") or 1500) / 1000
        return (
            f"真实路网：中心 {extent_km:.1f} 公里内 {spacing:.0f} 米方格，"
            f"逐格实测步行 {limit_km:.0f} 公里内能否到达"
        )
    scoped = "（较早的结果，只取其中落在圈内的格子）" if blindspots.get("scoped_from") else ""
    return (
        f"真实路网：15 分钟步行圈内 {spacing:.0f} 米方格，"
        f"逐格实测步行 {limit_km:.0f} 公里内能否到达{scoped}"
    )


def build_report(
    properties: dict[str, Any],
    coverage: dict[str, Any] | None = None,
    blindspots: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """由等时圈属性、可选的覆盖统计与网格盲区判定生成体检报告。

    网格盲区只读真实路网的判定结果（blindspot.py）。没有做网格判定时均衡层记为待测，
    不拿直线距离顶替——直线法恰恰会把「直线够近、走路绕远」的居民点漏判成有覆盖。
    """
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

    grid = blindspots or None
    cells = (grid or {}).get("cells") or []
    blind_ratio = grid.get("blind_ratio") if grid else None
    in_circle = [c for c in cells if c.get("in_circle", True)]

    r = reach_score(area, minutes, speed)
    c = compact_score(compactness)
    d = detour_score(mean_detour)
    uncertain_categories = []
    score_categories = dict(categories) if categories is not None else None
    if (
        coverage
        and (
            (coverage.get("fresh_breakdown") or {}).get("pending_in_circle", 0)
            or coverage.get("fresh_needs_refresh")
        )
        and not (categories or {}).get(FRESH, 0)
    ):
        uncertain_categories.append(FRESH)
        if score_categories is not None:
            score_categories.pop(FRESH, None)
    cov, blinds = cover_score(score_categories)
    proof_coverage = (
        {**coverage, "places": [p for p in coverage.get("places", []) if eligible(p)]}
        if coverage
        else None
    )
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
        # 路网可达一项的满分基准（理想方格路网的菱形），报告里用来说明评分口径
        "grid_area_km2": round(grid_area_km2(minutes, speed), 3),
        "area_ratio": round(area / ideal, 3) if ideal else 0,
        "straight_inflation": inflation,
        "categories": categories,
        # 检索失败的品类：数量未知，前端须显示为「查询失败」而非缺失
        "failed_categories": failed_categories,
        "blind_ratio": blind_ratio,
        "blind_cell_count": grid.get("blind_count") if grid else None,
        "cell_count": grid.get("cell_count") if grid else None,
        # 15 分钟圈内的格子单独计数：圈内也缺设施，比圈外缺更说明问题
        "blind_in_circle": sum(1 for c in in_circle if c.get("missing")) if cells else None,
        "cells_in_circle": len(in_circle) if cells else None,
        "grid_basis": grid_basis(grid),
        # 自动标注的灰色区域（相邻盲区格合并成片）与逐片成因诊断
        "gray_regions": gray_regions(grid, proof_coverage),
        "uncertain_categories": uncertain_categories,
        "coverage_source": coverage.get("source") if coverage else None,
        "coverage_pending": coverage is None,
        "blindspots_pending": grid is None,
        "prescriptions": prescribe(
            properties, proof_coverage, grid, blinds, [*failed_categories, *uncertain_categories]
        ),
    }
