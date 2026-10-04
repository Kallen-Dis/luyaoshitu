"""把体检结果写成一段可读的「综合结论」：四段话，全部由算法结果按模板生成。

1. 从结果里抽出一份**事实清单**（不含坐标），数字全部来自算法输出；
2. 按固定的四段结构写出来：【总体结论】【主要问题】【诊疗建议】【数据边界】。

不调用任何外部服务、不花配额，同一份结果永远得到同一段话；导出报告时同样按这份模板写在开头。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

_BEIJING = timezone(timedelta(hours=8))

ACTION_LABEL = {
    "connect": "打通",
    "site": "补设",
    "densify": "加密",
    "network": "路网",
    "maintain": "维持",
}
DIMENSION_LABEL = {
    "reach": "路网可达",
    "compact": "方向均衡",
    "detour": "路径效率",
    "cover": "设施覆盖",
    "equity": "可达均衡",
}
CAUSE_LABEL = {"barrier": "路网阻隔", "supply": "供给缺口", "unknown": "成因未知"}


def _short(name: str | None) -> str:
    return re.sub(r"^上海市[^区]+区", "", name or "") or "当前地点"


def build_facts(feature: dict[str, Any]) -> dict[str, Any]:
    """从一次结果里抽出写结论要用的事实。只放统计数字、名称与方向，不放坐标。"""
    p = feature.get("properties") or {}
    r = p.get("report") or {}
    cov = p.get("coverage") or {}
    minutes = float(p.get("minutes") or 15)
    facts: dict[str, Any] = {
        "地点": _short(p.get("name")),
        "出行方式": p.get("mode_label") or "步行",
        "时间阈值_分钟": int(minutes) if minutes.is_integer() else minutes,
        "离线模拟": bool(p.get("simulated")),
        "等时圈": {
            "面积_km2": p.get("area_km2"),
            "直线画圆面积_km2": r.get("ideal_area_km2"),
            "真实面积占直线圆_百分比": (
                round(float(r["area_ratio"]) * 100, 1) if r.get("area_ratio") else None
            ),
            "最短方向半径_米": round(float(p["min_radius_m"])) if p.get("min_radius_m") else None,
            "最远方向半径_米": round(float(p["max_radius_m"])) if p.get("max_radius_m") else None,
            "紧凑度": p.get("compactness"),
            "平均绕行系数": p.get("mean_detour"),
        },
        "体检评分": {
            "总分": r.get("total"),
            "等级": r.get("grade"),
            "路网三项的满分基准": "理想方格路网（不是直线画圆）",
            "各维度": {
                DIMENSION_LABEL[k]: v
                for k, v in (r.get("dimensions") or {}).items()
                if k in DIMENSION_LABEL
            },
        },
        "圈内设施数": r.get("categories") or cov.get("categories"),
        "检索半径内设施数": cov.get("nearby_categories"),
        "检索失败的品类": r.get("failed_categories") or [],
    }
    delay = p.get("delay") or {}
    if delay.get("applied"):
        facts["过街与路口等待"] = {
            "每公里等待_秒": delay.get("delay_per_km_s"),
            "不计等待时面积_km2": delay.get("raw_area_km2"),
            "计入等待后面积_km2": delay.get("area_km2"),
            "边界内过街次数": delay.get("boundary_crossings"),
        }
    if r.get("cell_count"):
        facts["网格盲区"] = {
            "口径": r.get("grid_basis"),
            "方格总数": r.get("cell_count"),
            "缺至少一类的方格": r.get("blind_cell_count"),
            "圈内方格": r.get("cells_in_circle"),
            "圈内缺设施的方格": r.get("blind_in_circle"),
            "各类缺失占比_百分比": {
                k: round(float(v) * 100, 1) for k, v in (r.get("blind_ratio") or {}).items()
            },
        }
    regions = ((r.get("gray_regions") or {}).get("regions")) or []
    if regions:
        facts["灰色区域"] = [
            {
                "编号": g.get("label"),
                "格数": g.get("cells"),
                "面积_km2": g.get("area_km2"),
                "圈内格数": g.get("in_circle_cells"),
                "各类成因": [
                    {
                        "品类": d.get("category"),
                        "缺格数": d.get("cells"),
                        "路网阻隔格": d.get("barrier_cells"),
                        "供给缺口格": d.get("supply_cells"),
                        "主因": CAUSE_LABEL.get(d.get("cause"), d.get("cause")),
                        "例": (
                            {
                                "方向": d["nearby"].get("direction"),
                                "直线_米": d["nearby"].get("straight_m"),
                                "步行_米": (
                                    round(d["nearby"]["walk_m"])
                                    if d["nearby"].get("walk_m")
                                    else None
                                ),
                                "设施": d["nearby"].get("place"),
                            }
                            if d.get("nearby")
                            else None
                        ),
                    }
                    for d in g.get("diagnosis") or []
                ],
            }
            for g in regions[:4]
            if g.get("id")
        ]
    prescriptions = r.get("prescriptions") or []
    if prescriptions:
        facts["诊疗处方"] = [
            {
                "类型": ACTION_LABEL.get(x.get("action"), x.get("action")),
                "品类": x.get("category"),
                "标题": x.get("title"),
                "说明": x.get("reason"),
            }
            for x in prescriptions[:6]
        ]
    if p.get("closures"):
        facts["用户标注的施工围挡数"] = len(p["closures"])
    warnings = [w.get("message") for w in p.get("warnings") or [] if w.get("message")]
    if warnings:
        facts["降级说明"] = warnings[:5]
    return facts


def template_text(facts: dict[str, Any]) -> str:
    """按四段结构写综合结论，每个数字都直接取自事实清单。"""
    iso = facts.get("等时圈") or {}
    score = facts.get("体检评分") or {}
    place = facts.get("地点")
    minutes = facts.get("时间阈值_分钟")
    mode = facts.get("出行方式")

    head = f"【总体结论】{place}的 {minutes} 分钟{mode}圈面积 {iso.get('面积_km2')} km²"
    if iso.get("真实面积占直线圆_百分比") is not None:
        head += f"，只有直线画圆面积的 {iso['真实面积占直线圆_百分比']}%"
    head += f"。体检总分 {score.get('总分')}（{score.get('等级')}）。"
    if facts.get("离线模拟"):
        head += "这是离线模拟数据，不代表真实路网，只用于演示。"
    delay = facts.get("过街与路口等待")
    if delay:
        raw_area = delay.get("不计等待时面积_km2")
        new_area = delay.get("计入等待后面积_km2")
        head += (
            f"计入过街与路口等待（每公里约 {delay.get('每公里等待_秒')} 秒）后，"
            f"圈面积由 {raw_area} 缩到 {new_area} km²。"
        )

    problems = []
    inside = facts.get("圈内设施数") or {}
    nearby = facts.get("检索半径内设施数") or {}
    zero = [k for k, v in inside.items() if v == 0]
    if zero:
        near = [f"{k} {nearby.get(k)} 处" for k in zero if nearby.get(k)]
        text = f"{minutes} 分钟圈内完全没有{'、'.join(zero)}"
        if near:
            text += f"；检索半径内有{'、'.join(near)}，但都走不进这个圈"
        problems.append(text + "。")
    grid = facts.get("网格盲区")
    if grid:
        blind_in, in_circle = grid.get("圈内缺设施的方格"), grid.get("圈内方格")
        problems.append(
            f"{grid.get('方格总数')} 个方格中 {grid.get('缺至少一类的方格')} 格步行 1 公里到不了"
            f"至少一类关键设施，其中圈内 {blind_in} / {in_circle} 格。"
        )
    for g in (facts.get("灰色区域") or [])[:2]:
        causes = "；".join(
            f"{d['品类']}缺 {d['缺格数']} 格，以{d['主因']}为主" for d in g.get("各类成因") or []
        )
        problems.append(
            f"{g.get('编号')} 共 {g.get('格数')} 格（约 {g.get('面积_km2')} km²）：{causes}。"
        )
    body = "【主要问题】" + ("".join(problems) or "关键设施步行覆盖基本均衡。")

    plans = facts.get("诊疗处方") or []
    advice = "【诊疗建议】" + (
        "".join(f"{i}. {x.get('标题')}。" for i, x in enumerate(plans[:4], start=1))
        or "维持现有布点，在施工或路口改造后复检。"
    )

    tail = (
        "【数据边界】等时圈与盲区都按百度真实路网实测；过街等待来自路线模型的统计延误，"
        "不是实时信号灯；打通与补设的效果是估算，选址需经路网核验与现场踏勘。"
    )
    failed = facts.get("检索失败的品类") or []
    if failed:
        tail += f"{'、'.join(failed)}检索失败，数量未知，未计入评分，也不开方。"
    for w in (facts.get("降级说明") or [])[:2]:
        tail += w if w.endswith("。") else w + "。"
    return "\n\n".join([head, body, advice, tail])


def narrate(feature: dict[str, Any]) -> dict[str, Any]:
    """生成综合结论。纯本地计算：不调外部服务，同一份结果永远得到同一段话。"""
    return {
        "text": template_text(build_facts(feature)),
        "generated_at": datetime.now(_BEIJING).isoformat(timespec="seconds"),
    }
