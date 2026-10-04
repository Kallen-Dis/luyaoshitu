"""面向用户的错误说明与降级提示。

一次实时分析分四段：坐标转换 → 等时圈 → 设施检索 → 网格盲区。任何一段出问题，
报错都要回答四件事：**哪个服务**、**为什么**、**在哪一步停下、之前的结果还在不在**、
**现在能做什么**。只说「配额耗尽」「AK 无效」会让人去查错方向——配额按服务独立计算，
地点检索用完时批量算路可能完全正常。

没有中断、但做了降级的情形（某品类检索失败、部分方格没测到、实时路况用了旧数据），
写进结果的 warnings，与报告一起展示。降级本身是正确的处置，但不能悄悄发生。
"""

from __future__ import annotations

from typing import Any

from .baidu.client import BaiduMapClient
from .baidu.errors import (
    BaiduApiError,
    ConfigurationError,
    IncompleteSamplingError,
    QuotaExhaustedError,
    config_hint,
    quota_hint,
    service_label,
)

STAGE_LABELS: dict[str, str] = {
    "center": "坐标转换",
    "isochrone": "等时圈采样",
    "coverage": "设施检索",
    "blindspots": "网格盲区判定",
    "report": "生成报告",
    "geocode": "地址解析",
    "simulate": "模拟新建",
    "recheck": "复测巡检",
    "construction": "工地检索",
    "site_plan": "核验选址",
    "markings": "叠加用户标注",
}

_FALLBACKS = (
    "现在可以：打开预生成样例（零配额）；对已经算过的地点重新计算（命中缓存不耗配额）；"
    "或切到「离线模拟」查看界面。"
)


def error_detail(
    exc: BaiduApiError,
    stage: str | None = None,
    completed: list[str] | None = None,
    retries: int = 3,
) -> tuple[int, dict[str, Any]]:
    """把百度接口错误翻译成 (HTTP 状态码, 结构化说明)。"""
    stage_label = STAGE_LABELS.get(stage or "", "")
    where = f"在「{stage_label}」这一步中断。" if stage_label else ""
    kept = f"已完成的{'、'.join(completed)}保留在地图上。" if completed else ""
    base = {
        "status": exc.status,
        "endpoint": exc.endpoint,
        "service": service_label(exc.endpoint),
        "stage": stage,
        "stage_label": stage_label or None,
    }

    if isinstance(exc, QuotaExhaustedError):
        if exc.endpoint.startswith("/geocoding"):
            action = "现在可以直接输入坐标，或勾选「允许点击地图重新计算」后在地图上选点。"
        else:
            action = _FALLBACKS
        return 429, {
            **base,
            "code": "quota_exhausted",
            "reset": "北京时间次日 0 点" if exc.status == 302 else None,
            "message": f"{quota_hint(exc)}。{where}{kept}{action}",
        }

    if isinstance(exc, ConfigurationError):
        missing = exc.status == 5 and "未配置" in str(exc)
        return (503 if missing else 500), {
            **base,
            "code": "missing_server_ak" if missing else "configuration_error",
            "message": f"{config_hint(exc)}。{where}{kept}".rstrip("。") + "。",
        }

    if isinstance(exc, IncompleteSamplingError):
        return 502, {
            **base,
            "code": "sampling_incomplete",
            "failures": exc.failures[:5],
            "message": (
                f"{service_label(exc.endpoint)}在等时圈采样时有 {len(exc.failures)} 批请求失败"
                f"（已自动重试 {retries} 次）：{str(exc).split('：', 1)[-1]}。"
                "没测到的点不是「走不到」，为避免把接口故障画成障碍，本次不出圈。"
                "这类故障通常是瞬时的，稍后重试即可；反复出现时请检查网络，或调低 BAIDU_MAX_QPS。"
            ),
        }

    return 502, {
        **base,
        "code": "upstream_error",
        "message": (
            f"{service_label(exc.endpoint)}请求失败（{exc}）。{where}{kept}"
            "这类故障通常是瞬时的，稍后重试即可。"
        ),
    }


class DegradationLog:
    """记录一次分析里各阶段的降级，最终写成 properties.warnings。"""

    def __init__(self, client: BaiduMapClient) -> None:
        self.client = client
        self.items: list[dict[str, str]] = []
        self._quota_mark = len(client.quota_events)

    def add(self, stage: str, message: str) -> None:
        self.items.append(
            {"stage": stage, "stage_label": STAGE_LABELS.get(stage, ""), "message": message}
        )

    def new_quota_services(self, prefix: str) -> list[dict[str, Any]]:
        """本次分析里新出现的、某类接口的配额耗尽事件。"""
        return [
            e
            for e in self.client.quota_events[self._quota_mark :]
            if str(e.get("endpoint", "")).startswith(prefix)
        ]

    def as_list(self) -> list[dict[str, str]]:
        return list(self.items)


def isochrone_warnings(log: DegradationLog, props: dict[str, Any]) -> None:
    delay = props.get("delay") or {}
    missed = int(delay.get("routes_requested") or 0) - int(delay.get("routes_ok") or 0)
    if missed > 0:
        if log.new_quota_services("/directionlite"):
            log.add(
                "isochrone",
                f"步行路线规划今天的配额已经用完，{missed} 个方向没有补过街与路口等待，"
                "这些方向的边界偏远、圈偏大。明天重算即可补上。",
            )
        else:
            log.add(
                "isochrone",
                f"{missed} 个方向没取到步行路线，按不含过街等待的耗时计算，这些方向的圈偏大。",
            )


def traffic_summary(
    log: DegradationLog, ages: list[float], stale: list[float], reason: str | None, ttl_s: float
) -> dict[str, Any] | None:
    if not ages:
        return None
    info = {
        "fresh_ttl_min": round(ttl_s / 60, 1),
        "max_age_min": round(max(ages) / 60, 1),
        "stale_points": len(stale),
    }
    if stale:
        info["stale_max_age_min"] = round(max(stale) / 60, 1)
        log.add(
            "isochrone",
            f"实时路况接口这次没有取到新数据（{(reason or '未知原因')[:80]}），"
            f"{len(stale)} 个采样点用了最近一次查到的路况，"
            f"最旧的是 {info['stale_max_age_min']} 分钟前。圈的形状反映的是那时的拥堵，不是此刻。",
        )
    return info


def coverage_warnings(log: DegradationLog, failed: list[str]) -> None:
    if not failed:
        return
    names = "、".join(failed)
    if log.new_quota_services("/place/v2/search"):
        log.add(
            "coverage",
            f"地点检索今天的配额已经用完（北京时间次日 0 点重置），{names}的数量未知："
            "没有计入评分，也不会被当成「缺失」。",
        )
    else:
        log.add(
            "coverage",
            f"{names}检索失败（网络或接口异常），数量未知：没有计入评分，也不会被当成「缺失」。",
        )


def blindspot_warnings(log: DegradationLog, blindspots: dict[str, Any]) -> None:
    cells = blindspots.get("cells") or []
    unknown = sum(1 for c in cells if c.get("unknown"))
    if unknown:
        if log.new_quota_services("/routematrix"):
            log.add(
                "blindspots",
                f"批量算路的配额在盲区判定途中用完，{unknown} 个方格没能完成判定，"
                "记为「未知」，既不算盲区也不算覆盖。明天重算即可补齐（已测部分命中缓存）。",
            )
        elif unknown >= max(5, len(cells) // 10):
            log.add(
                "blindspots",
                f"{unknown} 个方格没能完成判定（测距失败，或最近几家候选都走不到、轮数用尽），"
                "记为「未知」，不计入盲区占比。",
            )
    for name in blindspots.get("incomplete_categories") or []:
        log.add(
            "blindspots",
            f"「{name}」判定成功的方格不到一半，占比不可靠，没有计入评分。",
        )
    check = blindspots.get("closure_check") or {}
    if check.get("unverified_pairs"):
        log.add(
            "blindspots",
            f"有 {check['unverified_pairs']} 条可能经过施工围挡的路线没能核验，"
            "相关方格记为「未知」。",
        )
