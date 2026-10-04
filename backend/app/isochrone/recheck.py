"""复测巡检：跳过缓存重取各方向的步行路线，与上次存下的路线比，找出「疑似新增阻断」。

命题的痛点之一是「更新慢」：施工围挡今天立起来，静态普查要等下一轮才会发现。
百度不提供围挡或施工事件接口，但它的步行路线会随路网数据更新而改变——
某段路被封，规划出的路线就会绕开它。于是：

1. 每次步行分析都把 36 个方向的路线（终点、路网距离、化简折线）存进结果，作为基线；
2. 复测时对同样的起终点**跳过缓存**重新规划（约 36 次步行路线请求，不占批量算路的点对配额）；
3. 某方向的新路线**明显变长**，就沿旧路线找「从哪里开始不再重合、到哪里又重合」，
   这段被放弃的旧路线就是疑似受阻的位置，取其中点画一个疑似围挡圈；
4. 用户确认后，它直接变成现有的施工围挡参与计算（射线截断、围挡内设施不可用）。

这只能叫「疑似」：路线变长也可能是百度调整了路网数据、小区门禁时段变化，
第一次分析的地点也没有基线可比。结论必须由人确认。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..baidu.client import BaiduMapClient
from .geometry import (
    bearing_deg,
    distance_to_polyline_m,
    haversine_m,
    point_along,
    polyline_length_m,
    resample_polyline,
    simplify_path,
)
from .refine import MAX_CLOSURE_RADIUS_M, MIN_CLOSURE_RADIUS_M

_COMPASS = ("北", "东北", "东", "东南", "南", "西南", "西", "西北")
_BEIJING = timezone(timedelta(hours=8))


def compass(bearing: float) -> str:
    return _COMPASS[int(((bearing % 360) + 22.5) // 45) % 8]


class NoBaselineError(ValueError):
    """结果里没有可比的路线基线（旧版快照、离线模拟、非步行、未取路线）。"""


@dataclass(frozen=True)
class RecheckConfig:
    # 变长多少才算「明显」：绝对值与比例取大者。百度同一起终点两次规划偶有十几米的出入
    min_increase_m: float = 100.0
    min_increase_ratio: float = 0.08
    # 旧路线上的点离新路线超过这个距离，视为两条路线在这里分开了
    diverge_tol_m: float = 30.0
    # 沿旧路线取样的步长
    sample_step_m: float = 10.0
    # 相邻方向的疑似点落在同一处时合并（同一处封路常常让好几个方向一起绕路）
    merge_m: float = 80.0
    # 被放弃的旧路段超过这个长度，位置只能算粗略
    precise_span_m: float = 400.0


def _parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        t = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=_BEIJING)


def baselines(feature: dict[str, Any]) -> tuple[tuple[float, float], list[dict[str, Any]]]:
    """取出中心点与带路线基线的射线。没有可比基线时抛 NoBaselineError。"""
    props = feature.get("properties") or {}
    if props.get("simulated"):
        raise NoBaselineError("离线模拟结果没有真实路线，不能复测。")
    if (props.get("mode") or "walk") != "walk":
        raise NoBaselineError("复测巡检只对步行结果做：比较的是各方向的步行路线。")
    center = props.get("center") or {}
    if center.get("lat") is None or center.get("lng") is None:
        raise NoBaselineError("结果里没有中心点坐标。")
    rays = [
        r
        for r in props.get("rays") or []
        if r.get("route_to") and len(r.get("route_path") or []) >= 2 and r.get("route_m")
    ]
    if not rays:
        raise NoBaselineError(
            "这份结果没有保存各方向的步行路线（旧版快照，或计算时关闭了过街等待校正），"
            "无法对比。请先开启过街等待校正实时计算一次，以后就能对它复测。"
        )
    return (float(center["lat"]), float(center["lng"])), rays


def _route_points(route: dict[str, Any]) -> list[tuple[float, float]]:
    return [
        (float(p[0]), float(p[1]))
        for step in route.get("steps") or []
        for p in step.get("path") or []
    ]


def _lnglat(points: list[tuple[float, float]], tol: float = 4.0) -> list[list[float]]:
    return [[round(lng, 6), round(lat, 6)] for lat, lng in simplify_path(points, tol)]


def divergence(
    old: list[tuple[float, float]],
    new: list[tuple[float, float]],
    tol_m: float,
    step_m: float = 10.0,
) -> tuple[float, float] | None:
    """旧路线上「开始偏离新路线」与「重新汇合」的位置（沿旧路线的米数）。

    连续两个取样点都偏离才算分开、连续两个都贴合才算汇合，避免折线抖动造成误判。
    两条路线全程重合返回 None。
    """
    if len(old) < 2 or len(new) < 2:
        return None
    samples = resample_polyline(old, step_m)
    off = [distance_to_polyline_m(p, new) > tol_m for _, p in samples]
    start = next((k for k in range(len(off) - 1) if off[k] and off[k + 1]), None)
    if start is None:
        return None
    begin_s = samples[max(0, start - 1)][0]
    end_s = samples[-1][0]
    for k in range(start + 2, len(off) - 1):
        if not off[k] and not off[k + 1]:
            end_s = samples[k][0]
            break
    return begin_s, end_s


def compare_ray(
    base: dict[str, Any], fresh: dict[str, Any] | None, cfg: RecheckConfig
) -> dict[str, Any]:
    """比较一个方向的新旧路线。status：same / longer / shorter / rerouted / failed。"""
    old_pts = [(float(p[1]), float(p[0])) for p in base["route_path"]]
    old_m = float(base["route_m"])
    out: dict[str, Any] = {
        "bearing": float(base.get("bearing") or 0.0),
        "old_m": round(old_m),
        "new_m": None,
        "delta_m": None,
        "status": "failed",
    }
    if fresh is None:
        return out
    new_pts = _route_points(fresh)
    new_m = float(fresh.get("distance_m") or 0.0) or polyline_length_m(new_pts)
    delta = new_m - old_m
    threshold = max(cfg.min_increase_m, cfg.min_increase_ratio * old_m)
    span = divergence(old_pts, new_pts, cfg.diverge_tol_m, cfg.sample_step_m)
    if delta >= threshold:
        status = "longer"
    elif delta <= -threshold:
        status = "shorter"
    elif span is not None:
        status = "rerouted"
    else:
        status = "same"
    out.update(new_m=round(new_m), delta_m=round(delta), status=status)
    if status != "same":
        out["old_path"] = [list(p) for p in base["route_path"]]
        out["new_path"] = _lnglat(new_pts)
    if status == "longer" and span is not None:
        begin_s, end_s = span
        length = max(0.0, end_s - begin_s)
        mid = point_along(old_pts, (begin_s + end_s) / 2)
        segment = [point_along(old_pts, begin_s + length * k / 12) for k in range(13)]
        out["suspect"] = {
            "lat": mid[0],
            "lng": mid[1],
            "span_m": round(length),
            "begin_m": round(begin_s),
            "radius_m": round(
                max(MIN_CLOSURE_RADIUS_M + 15, min(120.0, length / 2, MAX_CLOSURE_RADIUS_M))
            ),
            "precise": length <= cfg.precise_span_m,
            "segment": [[round(lng, 6), round(lat, 6)] for lat, lng in segment],
        }
    return out


def _merge_suspects(
    center: tuple[float, float], rays: list[dict[str, Any]], cfg: RecheckConfig
) -> list[dict[str, Any]]:
    """把各方向的疑似点合并成若干处，按绕路增量从大到小编号。"""
    found = sorted((r for r in rays if r.get("suspect")), key=lambda r: -(r.get("delta_m") or 0))
    merged: list[dict[str, Any]] = []
    for r in found:
        s = r["suspect"]
        home = next(
            (
                m
                for m in merged
                if haversine_m(m["lat"], m["lng"], s["lat"], s["lng"]) <= cfg.merge_m
            ),
            None,
        )
        if home is not None:
            home["bearings"].append(r["bearing"])
            home["max_delta_m"] = max(home["max_delta_m"], r["delta_m"])
            continue
        merged.append(
            {
                "lat": round(s["lat"], 6),
                "lng": round(s["lng"], 6),
                "radius_m": s["radius_m"],
                "precise": s["precise"],
                "span_m": s["span_m"],
                "segment": s["segment"],
                "bearings": [r["bearing"]],
                "old_m": r["old_m"],
                "new_m": r["new_m"],
                "max_delta_m": r["delta_m"],
            }
        )
    for i, m in enumerate(merged, start=1):
        dist = haversine_m(center[0], center[1], m["lat"], m["lng"])
        side = compass(bearing_deg(center[0], center[1], m["lat"], m["lng"]))
        m["id"] = f"S{i}"
        m["distance_m"] = round(dist)
        m["direction"] = side
        m["label"] = f"复测疑似{m['id']}（{side}）"
        where = "位置较准" if m["precise"] else f"被放弃的旧路段长 {m['span_m']} 米，位置较粗"
        m["reason"] = (
            f"{side}侧 {len(m['bearings'])} 个方向的步行路线变长，最多由 {m['old_m']} 米变为 "
            f"{m['new_m']} 米（+{m['max_delta_m']} 米）。新路线在离中心约 {m['distance_m']} 米处"
            f"绕开了原来的走法（{where}）。可能是施工围挡、临时封路或门禁调整，需要现场确认。"
        )
    return merged


async def recheck_routes(
    client: BaiduMapClient,
    feature: dict[str, Any],
    cfg: RecheckConfig | None = None,
) -> dict[str, Any]:
    """对结果里每个有基线的方向跳过缓存重取路线并比较。"""
    cfg = cfg or RecheckConfig()
    center, rays = baselines(feature)
    props = feature.get("properties") or {}
    before = client.usage_snapshot()

    async def fetch(r: dict[str, Any]) -> dict[str, Any] | None:
        lat, lng = r["route_to"]
        return await client.walking_route(center, (float(lat), float(lng)), fresh=True)

    fresh = await asyncio.gather(*(fetch(r) for r in rays))
    after = client.usage_snapshot()
    results = [compare_ray(b, f, cfg) for b, f in zip(rays, fresh, strict=True)]
    suspects = _merge_suspects(center, results, cfg)

    counts = {k: 0 for k in ("same", "longer", "shorter", "rerouted", "failed")}
    for r in results:
        counts[r["status"]] += 1

    stamps = [t for r in rays if (t := _parse_time(r.get("route_fetched_at")))]
    base_time = min(stamps) if stamps else _parse_time(props.get("generated_at"))
    now = datetime.now(_BEIJING)
    requests = after["requests"].get("route", 0) - before["requests"].get("route", 0)
    return {
        "checked": len(results),
        **counts,
        "rays": results,
        "suspects": suspects,
        "improved": [
            {"bearing": r["bearing"], "old_m": r["old_m"], "new_m": r["new_m"]}
            for r in results
            if r["status"] == "shorter"
        ],
        "baseline": {
            "fetched_at": base_time.isoformat(timespec="seconds") if base_time else None,
            "age_days": round((now - base_time).total_seconds() / 86400, 1) if base_time else None,
        },
        "checked_at": now.isoformat(timespec="seconds"),
        "route_requests": requests,
        # 客户端是进程级共享的，只取本次调用期间新增的失败记录，最多列三条
        "route_failures": client.route_failures[before["route_failures"] : after["route_failures"]][
            :3
        ],
        "thresholds": {
            "min_increase_m": cfg.min_increase_m,
            "min_increase_ratio": cfg.min_increase_ratio,
            "diverge_tol_m": cfg.diverge_tol_m,
        },
        "note": (
            "跳过缓存重新规划了各方向的步行路线，与这份结果保存的路线比较。"
            "路线明显变长、且能定位到被放弃的旧路段时，标为「疑似新增阻断」；"
            "变长也可能来自百度的路网数据调整或门禁时段，需要人工确认后才作为围挡参与计算。"
        ),
    }
