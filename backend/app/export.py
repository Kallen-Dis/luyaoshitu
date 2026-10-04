"""成果导出：把一次体检打包成可验收的交付物。

只读快照与已生成的报告，零 API 消耗。评审当场拿到「一份报告 + 数据文件」
比屏幕截图更能体现工程完整度。

- Markdown 报告：面向人类阅读，结构与界面侧栏一致；
- CSV：盲区网格逐点明细，可在 Excel 里复核；
- GeoJSON：等时圈，可进任何 GIS 工具；
- JSON：完整 properties，机器可读；
- ZIP：以上全部打包，一次下载。
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from typing import Any

from .narrate import build_facts, template_text

# CSV 注入防护：单元格以这些字符开头时前缀单引号，
# 防止导出的文件在 Excel 中被当作公式执行。
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    if text.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def export_name(payload: dict[str, Any], ext: str) -> str:
    """由样例名生成安全文件名。只保留中英文数字与常用符号。"""
    props = payload.get("properties", {})
    raw = str(props.get("name") or "analysis")
    safe = "".join(ch if ch.isalnum() or "\u4e00" <= ch <= "\u9fff" else "_" for ch in raw)
    safe = safe.strip("_")[:32] or "analysis"
    return f"路遥识途_{safe}_体检报告.{ext}"


def build_markdown(payload: dict[str, Any]) -> str:
    """生成 Markdown 体检报告。结构与侧栏一致，可直接提交为交付文档。"""
    props = payload.get("properties", {})
    report = props.get("report") or {}
    coverage = props.get("coverage") or {}
    blindspots = props.get("blindspots") or {}
    quality = props.get("quality") or {}
    center = props.get("center") or {}

    name = props.get("name") or "未命名"
    minutes = props.get("minutes", 15)
    mode_label = props.get("mode_label") or "步行"
    coord_sys = props.get("input_coord_sys")
    coord_note = f"（{coord_sys.upper()} 输入）" if coord_sys and coord_sys != "bd09" else ""

    lines = [
        f"# 路遥识途｜{name} 15 分钟生活圈体检报告",
        "",
        f"- 中心点：({center.get('lat', '-')}, {center.get('lng', '-')}，BD09){coord_note}",
        f"- 时间阈值：{minutes} 分钟（{mode_label}）",
        f"- 生成时间：{props.get('generated_at') or '实时计算'}",
    ]
    if props.get("simulated"):
        # 离线模拟结果由伪随机生成，导出后脱离界面也必须看得出来
        lines.append("- **离线模拟结果：由确定性伪随机生成，不代表真实路网，不能作为结论**")
    # 综合结论按模板由算法结果生成，和页面上显示的是同一段话；不依赖前端有没有先生成过
    conclusion = template_text(build_facts(payload))
    lines += ["", "## 综合结论", "", conclusion]
    lines += [
        "",
        "## 真实路网 vs 直线画圆",
        "",
    ]
    area = props.get("area_km2")
    if area is not None:
        ideal = report.get("ideal_area_km2")
        ratio = report.get("area_ratio")
        lines += [
            f"- 真实路网可达面积：{area} km²",
            f"- 直线画圆面积：{ideal if ideal is not None else '-'} km²",
        ]
        if ratio is not None and ideal:
            lines.append(f"- 真实面积占比：{ratio * 100:.0f}%")
        if report.get("straight_inflation"):
            lines.append(f"- 直线口径高估：{report['straight_inflation']} 倍")
    lines += [
        f"- 平均半径：{props.get('mean_radius_m', '-')} 米",
        f"- 最短方向：{props.get('min_radius_m', '-')} 米",
        f"- 最远方向：{props.get('max_radius_m', '-')} 米",
        f"- 紧凑度：{props.get('compactness', '-')}（明显偏低意味着铁路、河道等切割）",
        f"- 平均绕行系数：{props.get('mean_detour') or '-'}",
    ]

    dims = report.get("dimensions") or {}
    dim_label = {
        "reach": "路网可达",
        "compact": "方向均衡",
        "detour": "路径效率",
        "cover": "设施覆盖",
        "equity": "可达均衡",
    }
    lines += [
        "",
        "## 体检评分",
        "",
        f"**总分 {report.get('total', '-')} / 100（{report.get('grade', '-')}）**",
        "",
        "| 维度 | 得分 |",
        "| --- | ---: |",
    ]
    for key, label in dim_label.items():
        value = dims.get(key)
        lines.append(f"| {label} | {value if value is not None else '待采集'} |")
    if report.get("grid_area_km2"):
        lines += [
            "",
            "路网可达、方向均衡、路径效率三项以理想方格路网为满分（同样时间走得到 "
            f"{report['grid_area_km2']} km² 的菱形、最短与最长方向半径之比 0.71、"
            "平均绕行 1.27 倍）；直线画圆是任何路网都到不了的上界，只用来说明直线法高估了几倍。",
        ]

    categories = report.get("categories") or {}
    nearby = coverage.get("nearby_categories") or {}
    lines += [
        "",
        "## 圈内各类民生设施覆盖",
        "",
        f"| 品类 | {minutes} 分钟圈内 | 检索半径内（含圈外） | 判定 |",
        "| --- | ---: | ---: | --- |",
    ]
    for cat, count in categories.items():
        near = nearby.get(cat, count)
        verdict = "有覆盖" if count > 0 else ("附近有、走不进圈" if near > 0 else "缺失")
        lines.append(f"| {cat} | {count} | {near} | {verdict} |")
    for cat in report.get("failed_categories") or []:
        lines.append(f"| {cat} | — | — | 查询失败（数量未知，不按缺失计） |")
    in_count = sum(int(v) for v in categories.values())
    nearby_count = sum(int(v) for v in nearby.values())
    if nearby_count:
        outside = max(0, nearby_count - in_count)
        lines.append(
            f"| **合计** | **{in_count}** | **{nearby_count}** "
            f"| 另有 {outside} 处在附近但走不进等时圈 |"
        )

    delay = props.get("delay") or {}
    if delay.get("applied"):
        lines += [
            "",
            "## 过街与路口等待校正",
            "",
            f"- 来源：{delay.get('source', '')}",
            f"- 路线：{delay.get('routes_ok', 0)} / {delay.get('routes_requested', 0)} 个方向取到",
            f"- 平均每公里多出：{delay.get('delay_per_km_s', '-')} 秒",
            f"- 边界以内的过街次数（各方向合计）：{delay.get('boundary_crossings', 0)}",
            f"- 校正前面积 {delay.get('raw_area_km2', '-')} km²，"
            f"校正后 {delay.get('area_km2', '-')} km²",
            f"- 说明：{delay.get('note', '')}",
        ]
    closures = props.get("closures") or []
    if closures:
        lines += ["", "## 施工围挡（用户标注）", ""]
        for c in closures:
            lines.append(
                f"- ({c.get('lat')}, {c.get('lng')}) 半径 {c.get('radius_m')} 米"
                + (f"：{c['label']}" if c.get("label") else "")
            )
        lines.append(
            f"- 被围挡截断的方向：{delay.get('closure_rays', 0)} 个。"
            "接口不能绕开围挡重新规划，受阻按保守口径处理，需现场复核。"
        )

    if blindspots:
        lines += [
            "",
            "## 网格盲区",
            "",
            f"口径：{report.get('grid_basis') or '真实路网'}。",
            "",
            f"- 判定网格：{report.get('cell_count')} 个，"
            f"其中盲区 {report.get('blind_cell_count')} 个",
        ]
        if report.get("cells_in_circle") is not None:
            lines.append(
                f"- 落在 15 分钟圈内的网格：{report.get('cells_in_circle')} 个，"
                f"其中盲区 {report.get('blind_in_circle')} 个"
            )
        ratio = report.get("blind_ratio") or {}
        if ratio:
            lines += ["", "| 品类 | 不可达网格占比 |", "| --- | ---: |"]
            for cat, r in ratio.items():
                lines.append(f"| {cat} | {r * 100:.1f}% |")
        lines.append("")
        lines.append("占比为该品类步行 1 公里不可达的网格比例，测距失败的网格不计入分母。")

    if report.get("blinds"):
        lines += ["", "圈内完全缺失的品类：" + "、".join(report["blinds"]) + "。"]

    regions = (report.get("gray_regions") or {}).get("regions") or []
    if regions:
        lines += [
            "",
            "## 灰色区域（设施匮乏，自动标注）",
            "",
            f"共 {len(regions)} 片。{(report.get('gray_regions') or {}).get('basis', '')}。",
            "",
            "| 区域 | 格数 | 面积 | 圈内格数 | 缺失与成因 |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
        cause_text = {"barrier": "路网阻隔", "supply": "供给缺口", "unknown": "成因未知"}
        for r in regions:
            causes = "；".join(
                f"{d['category']} {d['cells']} 格（{cause_text.get(d['cause'], d['cause'])}）"
                for d in r.get("diagnosis") or []
            )
            lines.append(
                f"| {r.get('label')} | {r.get('cells')} | {r.get('area_km2')} km² "
                f"| {r.get('in_circle_cells')} | {causes} |"
            )
        lines.append("")
        for r in regions:
            if r.get("id"):
                lines.append(f"- {r.get('summary')}")

    prescriptions = report.get("prescriptions") or []
    if prescriptions:
        action_label = {
            "connect": "打通",
            "site": "补设",
            "densify": "加密",
            "network": "路网",
            "maintain": "维持",
        }
        lines += ["", "## 自动诊疗（规划建议）", ""]
        for p in prescriptions:
            action = action_label.get(p.get("action"), p.get("action", ""))
            head = f"- **[{action}]{p.get('category') or ''}**"
            if p.get("covers"):
                head += f"（覆盖约 {p['covers']} 个居民点）"
            lines.append(head)
            lines.append(f"  - {p.get('title', '')}")
            lines.append(f"  - {p.get('reason', '')}")

    lines += _markings_section(props.get("markings") or {})

    lines += ["", "## 算法质量", ""]
    lines.append(
        f"- 采样 {props.get('sampled_points', '-')} 点，"
        f"其中 {props.get('failed_points', '-')} 点算路失败"
    )
    if quality:
        lines.append(
            f"- 方向诊断：共 {quality.get('directions', '-')} 个方向，"
            f"屏障截断 {quality.get('barrier_truncated', '-')}，"
            f"首点即不可达 {quality.get('zero_radius', '-')}"
        )
        if quality.get("saturated") is not None:
            lines.append(
                f"- 采样上界内未超时（饱和）方向：{quality['saturated']}，" f"真实边界可能更远"
            )

    lines += [
        "",
        "## 方法与数据边界",
        "",
        "- 等时圈：扇形采样 + 批量算路 + 射线插值。失败点视为障碍截断射线；"
        "首次超阈值区间线性插值反解边界。",
        "- 过街等待：批量算路的耗时不含等待，每个方向另取一条步行路线，"
        "把步骤里超出匀速步行的秒数按路线距离补回；施工围挡为用户标注，路线穿过即截断。",
        "- 盲区判定：逐格实测步行距离；直线距离只作下界用于免费剪枝，"
        "从不用来判定「够得着」。够近就停 + 按候选分组装箱控制点对数。",
        "- 查询失败的品类判为「未知」，不按缺失计，避免 API 故障伪装成服务盲区。",
        "",
        f"数据来源：{report.get('coverage_source') or '预生成快照（零 API 消耗）'}。",
        "",
    ]
    return "\n".join(lines)


_MARKING_STATUS = {"verified": "已核实", "pending": "待核实"}


def _markings_section(markings: dict[str, Any]) -> list[str]:
    """用户标注：用了哪几条（含版本，结果可追溯）、各自的影响、与纯算法结果的差别。"""
    applied = markings.get("applied") or []
    suggested = markings.get("suggested") or []
    if not applied and not suggested:
        return []
    lines = ["", "## 用户标注", ""]
    effect = markings.get("effect")
    if effect:

        def signed(value: Any, unit: str = "") -> str:
            if value is None:
                return "—"
            return f"0{unit}" if value == 0 else f"{value:+g}{unit}"

        lines.append(
            f"- 纯算法 {effect.get('score_algorithm')} 分，叠加标注后 "
            f"{effect.get('score_with_markings')} 分（{signed(effect.get('score_delta'))}）；"
            f"盲区方格变化 {signed(effect.get('blind_cells_delta'))}，"
            f"等时圈面积变化 {signed(effect.get('area_delta_km2'), ' km²')}"
        )
    if applied:
        lines += ["", "| 编号 | 版本 | 状态 | 标注 | 影响 |", "| --- | --- | --- | --- | --- |"]
        for m in applied:
            status = _MARKING_STATUS.get(m.get("status"), m.get("status"))
            mine = "（自己的）" if m.get("mine") else ""
            lines.append(
                f"| #{m.get('id')} | v{m.get('version')} | {status}{mine} "
                f"| {_md_cell(m.get('title'))} | {_md_cell(m.get('effect'))} |"
            )
    if suggested:
        lines.append("")
        lines.append(f"另有 {len(suggested)} 条他人提交、尚未核实的标注只作为建议，未计入本报告。")
    skipped = markings.get("skipped") or []
    for s in skipped:
        lines.append(f"- 未使用 #{s.get('id')} {_md_cell(s.get('title'))}：{s.get('reason')}")
    lines.append("")
    lines.append(
        "> 用户标注由使用者提交；已核实的经管理员依据照片或实地信息审核。"
        "标注只改变输入（围挡、设施集合），判定仍按真实路网步行 1 公里。"
    )
    return lines


def _md_cell(value: Any) -> str:
    return str(value or "").replace("|", "／").replace("\n", " ")


def build_csv(payload: dict[str, Any]) -> str:
    """盲区网格明细 CSV：每个网格一行，含逐品类最近设施距离与判定结果。"""
    props = payload.get("properties", {})
    blindspots = props.get("blindspots") or {}
    cells = blindspots.get("cells") or []

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "lat",
            "lng",
            "是否在15分钟圈内",
            "中心点步行耗时秒（含过街等待）",
            "批量算路耗时秒（不含等待）",
            "最近设施步行距离米",
            "判为盲区的品类",
            "无法判定的品类",
            "受施工围挡影响",
        ]
    )
    for cell in cells:
        nearest = cell.get("nearest_m") or {}
        writer.writerow(
            [
                safe_cell(value)
                for value in (
                    cell.get("lat", ""),
                    cell.get("lng", ""),
                    "是" if cell.get("in_circle", True) else "否",
                    cell.get("reach_s", ""),
                    cell.get("reach_raw_s", ""),
                    ";".join(f"{k}={v}" for k, v in nearest.items() if v is not None),
                    "、".join(cell.get("missing") or []),
                    "、".join(cell.get("unknown") or []),
                    "是" if cell.get("closure_blocked") else "",
                )
            ]
        )
    return buf.getvalue()


def export_bundle(payload: dict[str, Any]) -> bytes:
    """ZIP 打包：Markdown 报告 + GeoJSON + 盲区 CSV + 完整 JSON。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("体检报告.md", build_markdown(payload))
        zf.writestr(
            "等时圈.geojson",
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
        zf.writestr("盲区网格.csv", build_csv(payload))
        zf.writestr(
            "完整数据.json",
            json.dumps(payload.get("properties", {}), ensure_ascii=False, indent=2),
        )
    return buf.getvalue()
