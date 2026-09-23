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

    lines += ["", "## 设施覆盖", "", "| 品类 | 圈内数量 |", "| --- | ---: |"]
    categories = report.get("categories") or {}
    for cat, count in categories.items():
        lines.append(f"| {cat} | {count} |")
    for cat in report.get("failed_categories") or []:
        lines.append(f"| {cat} | 查询失败（未知，不按缺失计） |")
    nearby = (coverage.get("nearby_categories") or {})
    in_count = sum(int(v) for v in categories.values())
    nearby_count = sum(int(v) for v in nearby.values())
    if nearby_count:
        lines.append(
            f"| **圈内合计** | **{in_count}**（另有 {max(0, nearby_count - in_count)} 处在附近但走不进等时圈） |"
        )

    if blindspots:
        lines += [
            "",
            "## 网格盲区",
            "",
            f"在等时圈内按 {blindspots.get('grid_spacing_m', 150)} 米布网格，逐点判定"
            f"步行 {int(blindspots.get('walk_limit_m', 1000))} 米内能否到达关键设施。",
            "",
            f"- 判定网格：{report.get('cell_count')} 个，其中盲区 {report.get('blind_cell_count')} 个",
        ]
        ratio = report.get("blind_ratio") or {}
        if ratio:
            lines += ["", "| 品类 | 不可达网格占比 |", "| --- | ---: |"]
            for cat, r in ratio.items():
                lines.append(f"| {cat} | {r * 100:.1f}% |")
        lines.append("")
        lines.append("占比为该品类步行 1 公里不可达的网格比例，测距失败的网格不计入分母。")

    if report.get("blinds"):
        lines += ["", "圈内完全缺失的品类：" + "、".join(report["blinds"]) + "。"]

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
                f"- 采样上界内未超时（饱和）方向：{quality['saturated']}，"
                f"真实边界可能更远"
            )

    lines += [
        "",
        "## 方法与数据边界",
        "",
        "- 等时圈：扇形采样 + 批量算路 + 射线插值。失败点视为障碍截断射线；"
        "首次超阈值区间线性插值反解边界。",
        "- 盲区判定：直线距离是步行距离的下界，据此免费剪枝；"
        "够近就停 + 按候选分组装箱控制点对数。",
        "- 查询失败的品类判为「未知」，不按缺失计，避免 API 故障伪装成服务盲区。",
        "",
        f"数据来源：{report.get('coverage_source') or '预生成快照（零 API 消耗）'}。",
        "",
    ]
    return "\n".join(lines)


def build_csv(payload: dict[str, Any]) -> str:
    """盲区网格明细 CSV：每个网格一行，含逐品类最近设施距离与判定结果。"""
    props = payload.get("properties", {})
    blindspots = props.get("blindspots") or {}
    coverage = props.get("coverage") or {}
    cells = blindspots.get("cells") or []

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "lat",
            "lng",
            "中心点步行耗时秒",
            "最近设施距离米",
            "判为盲区的品类",
            "无法判定的品类",
        ]
    )
    for cell in cells:
        nearest = cell.get("nearest_m") or {}
        writer.writerow(
            [
                cell.get("lat", ""),
                cell.get("lng", ""),
                cell.get("reach_s", ""),
                ";".join(f"{k}={v}" for k, v in nearest.items() if v is not None),
                "、".join(cell.get("missing") or []),
                "、".join(cell.get("unknown") or []),
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
