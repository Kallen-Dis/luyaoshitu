"""FastAPI 应用入口。

对外暴露等时圈计算与示例数据两条路径。默认走示例数据，实时计算需显式请求：
快照零 API 消耗，首屏不受配额与网络波动影响。

浏览器端 AK 通过 /api/config 下发而非编译进前端产物，这样换 Key 无需重新构建，
也便于在部署时通过环境变量覆盖。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Header, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import export, samples, storage
from .baidu.client import BaiduMapClient
from .baidu.errors import MISSING_AK_MESSAGE, BaiduApiError, quota_hint
from .config import get_settings
from .demo import build_demo_feature
from .diagnostics import (
    STAGE_LABELS,
    DegradationLog,
    blindspot_warnings,
    coverage_warnings,
    error_detail,
    isochrone_warnings,
    traffic_summary,
)
from .isochrone.algorithm import IsochroneConfig, compute_isochrone
from .isochrone.recheck import NoBaselineError, baselines, recheck_routes
from .isochrone.refine import (
    MAX_CLOSURE_RADIUS_M,
    MAX_CLOSURES,
    MIN_CLOSURE_RADIUS_M,
    RefineOptions,
    closures_from,
)
from .markings import apply as marking_apply
from .markings.routes import get_service as marking_service
from .markings.routes import install as install_markings
from .markings.routes import markings_for_analysis
from .narrate import narrate
from .poi import crosscheck
from .poi.catalog import CATEGORIES
from .poi.collect import collect_coverage
from .poi.construction import find_construction
from .poi.fresh import FRESH, VERSION, annotate_coverage
from .report.blindspot import BlindspotConfig, identify_blindspots, scope_to_circle
from .report.score import build_report
from .report.simulate import simulate_facility
from .report.siteplan import DEFAULT_BUDGET_PAIRS, plan_site
from .travel import MODES, WALK, TravelMode, get_mode
from .trip.candidates import place_id as trip_place_id
from .trip.routes import install as install_trip
from .trip.service import config as trip_config


class ClosureIn(BaseModel):
    """用户在地图上标注的施工围挡，以圆近似。"""

    lat: float = Field(..., ge=-90, le=90)
    lng: float = Field(..., ge=-180, le=180)
    radius_m: float = Field(50.0, ge=MIN_CLOSURE_RADIUS_M, le=MAX_CLOSURE_RADIUS_M)
    label: str = Field("", max_length=40)


class MarkingOptions(BaseModel):
    """auto：已核实的 + 自己的 + include；all：连他人待核实的也用；none：纯算法。"""

    mode: str = Field("auto", pattern="^(auto|all|none)$")
    include: list[int] = Field(default_factory=list, max_length=50)
    exclude: list[int] = Field(default_factory=list, max_length=50)


class IsochroneRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90, description="中心点纬度")
    lng: float = Field(..., ge=-180, le=180, description="中心点经度")
    coord_sys: str = Field(
        "bd09",
        description="输入坐标系：bd09（百度）/ gcj02（高德、腾讯）/ wgs84（GPS）",
    )
    minutes: float = Field(15.0, gt=0, le=60, description="时间阈值（分钟）")
    directions: int = Field(36, ge=8, le=72, description="射线方向数，越多越精细")
    coverage: bool = Field(True, description="是否采集圈内民生设施覆盖（消耗地点检索配额）")
    blindspots: bool = Field(
        True, description="是否做网格级盲区判定（消耗批量算路，且依赖 coverage）"
    )
    grid_spacing_m: float = Field(
        100.0,
        ge=50,
        le=400,
        description="盲区网格间距（只在 15 分钟圈内布点），越小越精细也越耗配额",
    )
    grid_extent_m: float = Field(
        1500.0,
        ge=500,
        le=2000,
        description="设施检索至少罩住的半径（以中心为圆心，再加 1 公里判定阈值）",
    )
    crossing_delay: bool = Field(
        True,
        description=(
            "步行时每个方向补一条步行路线，把过街与路口等待补回耗时（每次约 36 次路线规划）"
        ),
    )
    closures: list[ClosureIn] = Field(
        default_factory=list,
        max_length=MAX_CLOSURES,
        description="施工围挡（圆心 + 半径）。路线穿过围挡的方向按受阻处理",
    )
    mode: str = Field(
        "walk",
        description="出行方式：walk / ride / drive / drive_traffic",
    )
    name: str | None = Field(
        None,
        max_length=60,
        description="显示名：搜索时输入的地址或小区名，写进结果；不填时界面显示「当前地点」",
    )
    markings: MarkingOptions = Field(
        default_factory=lambda: MarkingOptions(),
        description="附近用户标注的使用方式",
    )


class SimulateRequest(BaseModel):
    """模拟新建：在指定位置放一处设施，按路网重算被消去的盲区方格。"""

    category: str = Field(..., min_length=1, max_length=20, description="拟建设施品类")
    lat: float = Field(..., ge=-90, le=90)
    lng: float = Field(..., ge=-180, le=180)
    feature: dict = Field(..., description="当前分析结果的完整 Feature")
    verify: bool = Field(True, description="是否对候选方格做真实路网测距（消耗少量批量算路点对）")


class ClosurePreviewRequest(BaseModel):
    feature: dict
    closures: list[ClosureIn] = Field(default_factory=list, max_length=MAX_CLOSURES)


class FeatureRequest(BaseModel):
    feature: dict = Field(..., description="当前分析结果的完整 Feature")


class ConstructionRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90, description="检索中心纬度（BD09）")
    lng: float = Field(..., ge=-180, le=180, description="检索中心经度（BD09）")
    radius_m: int = Field(1500, ge=300, le=3000, description="检索半径（米）")
    feature: dict | None = Field(
        None, description="可选：当前结果。带上时按各方向步行路线给候选排序"
    )


class SitePlanRequest(BaseModel):
    feature: dict = Field(..., description="当前分析结果的完整 Feature")
    category: str = Field(..., min_length=1, max_length=20)
    alternatives: int = Field(3, ge=1, le=5, description="核验几个备选点位")
    budget_pairs: int = Field(
        DEFAULT_BUDGET_PAIRS, ge=0, le=600, description="本次最多消耗的批量算路点对"
    )


class NarrateRequest(BaseModel):
    feature: dict = Field(..., description="当前分析结果的完整 Feature")


class CrosscheckRequest(BaseModel):
    feature: dict = Field(..., description="当前分析结果的完整 Feature")
    region: str | None = Field(
        None, max_length=4, description="只核对某一片灰色区域（编号，如 A）；不填则逐片核对"
    )


logger = logging.getLogger("luyaoshitu")

# AI 二次核对的进程级闸门：Agent Plan 按调用扣额度，接口又对所有人开放，
# 每小时实发的问题数封顶（命中缓存的不计）
crosscheck_quota = crosscheck.HourlyQuota(limit=60)

BROWSER_AK_MISSING = (
    "未配置浏览器端 AK（VITE_BAIDU_BROWSER_AK）：地图底图无法加载，报告与样例数据仍可查看。"
    "请在 .env 填入「浏览器端」类型的 AK（Referer 白名单写 localhost/*）后重启后端。"
)


def _config_warnings() -> list[dict[str, str]]:
    """启动配置问题。缺 AK 时后端照常启动，但前端与日志都必须明确报出来。"""
    s = get_settings()
    out: list[dict[str, str]] = []
    if not s.server_ak or s.server_ak.startswith("your_"):
        out.append({"code": "missing_server_ak", "message": MISSING_AK_MESSAGE})
    if not s.browser_ak or s.browser_ak.startswith("your_"):
        out.append({"code": "missing_browser_ak", "message": BROWSER_AK_MISSING})
    return out


@asynccontextmanager
async def lifespan(app: FastAPI):
    """进程级共享百度客户端。

    连接池复用能省下每次请求的 TLS 握手（实测约 1 秒），
    令牌桶也必须是进程唯一的，否则多个客户端各自限速会突破总配额。
    """
    for item in _config_warnings():
        logger.warning(item["message"])
    async with BaiduMapClient() as client:
        app.state.baidu = client
        storage.init_db()
        yield


app = FastAPI(
    title="路遥识途——基于百度地图的生活圈智能规划",
    description="基于百度地图开放能力，计算真实路网步行等时圈并诊断民生设施服务盲区",
    version="0.2.0",
    lifespan=lifespan,
)

# 开发期允许 Vite 开发服务器跨域访问；生产部署时前端与后端同源，可收紧此配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    # 标注的修改、撤回、投票分别用 PATCH / DELETE / PUT
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    allow_headers=["*"],
)

install_markings(app)


def _handle_baidu_error(
    exc: BaiduApiError, stage: str | None = None, completed: list[str] | None = None
) -> HTTPException:
    """把百度的错误分类翻译成合适的 HTTP 状态与具体说明（见 diagnostics.error_detail）。

    三类错误对用户意味着完全不同的事情：配额耗尽要等次日，配置错误要改部署，
    其余才是可重试的临时故障。笼统地返回 500 会让使用者无从判断。
    """
    status, detail = error_detail(exc, stage, completed, get_settings().max_retries)
    return HTTPException(status_code=status, detail=detail)


def _quota_delta(before: dict, after: dict, blind: dict | None) -> dict[str, int | None]:
    """由前后快照差值得出「本次计算」的配额消耗，并合并盲区剪枝统计。

    客户端是进程级单例，原始计数都是累计值；盲区层的直线剪枝统计来自
    identify_blindspots，它回答的是「朴素做法本来要花多少」。
    """
    req_before: dict[str, int] = before["requests"]
    req_after: dict[str, int] = after["requests"]

    def delta(label: str) -> int:
        return max(0, req_after.get(label, 0) - req_before.get(label, 0))

    return {
        "matrix_pairs": after["matrix_pairs"] - before["matrix_pairs"],
        "matrix_requests": delta("matrix"),
        "matrix_failed_blocks": after["matrix_failures"] - before["matrix_failures"],
        "matrix_concurrency": get_settings().matrix_concurrency,
        "route_requests": delta("route"),
        "route_failed": after.get("route_failures", 0) - before.get("route_failures", 0),
        "poi_queries": delta("poi"),
        "geocode_queries": delta("geocode"),
        "geoconv_queries": delta("geoconv"),
        "naive_matrix_pairs": blind.get("naive_matrix_pairs") if blind else None,
        "pruned_decisions": blind.get("pruned_decisions") if blind else None,
        "grid_cells": blind.get("cell_count") if blind else None,
    }


def _quality_from_rays(props: dict) -> dict[str, int | None]:
    """从快照的射线表推导质量诊断。快照没有保存饱和标记，
    该项置 None，前端据此只展示能确定的部分。"""
    if props.get("quality"):
        return props["quality"]
    rays = props.get("rays") or []
    return {
        "directions": len(rays),
        "saturated": None,
        "barrier_truncated": sum(1 for r in rays if r.get("barrier")),
        "closure_truncated": sum(1 for r in rays if r.get("closure")),
        "zero_radius": sum(1 for r in rays if (r.get("radius_m") or 0) <= 0),
        "route_failed": sum(1 for r in rays if r.get("route") == "failed"),
    }


async def _resolve_center(client: BaiduMapClient, req: IsochroneRequest) -> tuple[float, float]:
    """把请求坐标统一到 BD09，返回 (lat, lng)。geoconv 无日配额限制。"""
    center = (req.lat, req.lng)
    if req.coord_sys == "bd09":
        return center
    try:
        converted = await client.geoconv([center], from_sys=req.coord_sys)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "bad_coord_sys", "message": str(exc)},
        ) from exc
    except BaiduApiError as exc:
        raise _handle_baidu_error(exc, "center") from exc
    if not converted:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "upstream_error",
                "message": "坐标转换返回为空，请稍后重试或改用 BD09 坐标输入。",
            },
        )
    return converted[0]


def _get_mode_or_400(mode_id: str) -> TravelMode:
    try:
        return get_mode(mode_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "bad_mode", "message": str(exc)},
        ) from exc


class AnalysisState:
    """分析进行到哪一步、哪些阶段已经完成——出错时据此写出「停在哪、什么还在」。"""

    def __init__(self) -> None:
        self.stage = "center"
        self.completed: list[str] = []

    def enter(self, stage: str) -> None:
        self.stage = stage

    def finish(self, label: str) -> None:
        self.completed.append(label)


async def _analysis_steps(
    client: BaiduMapClient,
    req: IsochroneRequest,
    mode: TravelMode,
    center: tuple[float, float],
    state: AnalysisState,
    device_hash: str | None = None,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """一次完整分析：等时圈 → 设施覆盖 → 网格盲区。按阶段产出 (事件名, 数据)。

    流式接口把每个阶段原样推给前端；非流式接口消费完再一次性返回。
    三段的配额消耗量级完全不同，故各自可关，降级的粒度也就是这三段。
    没有中断但做了降级的情形写进 properties.warnings，与报告一起展示。
    """
    closures = closures_from([c.model_dump() for c in req.closures])
    walking = mode.id == WALK.id
    log = DegradationLog(client)
    traffic_mark = (len(client.traffic_ages), len(client.traffic_stale))
    blind_cfg = BlindspotConfig(grid_spacing_m=req.grid_spacing_m, extent_m=req.grid_extent_m)

    # 附近的用户标注：查本地库，零配额。结果 A 是纯算法，结果 B 叠加了要用的标注；
    # 没有要改变输入的标注时 B 就是 A，与没有这个功能时完全一样
    query_radius = blind_cfg.extent_m + blind_cfg.walk_limit_m
    nearby = await markings_for_analysis(center[0], center[1], query_radius, device_hash)
    mplan = marking_apply.plan(
        nearby,
        req.markings.mode,
        req.markings.include,
        req.markings.exclude,
        walking=walking,
    )
    merged, closure_ids = (
        marking_apply.merge_closures(closures, mplan) if walking else (closures, {})
    )
    network_changed = bool(closure_ids)
    refine = RefineOptions(delay=req.crossing_delay, closures=closures)
    refine_b = RefineOptions(delay=req.crossing_delay, closures=merged)

    state.enter("isochrone")
    shared = f"，并叠加 {len(closure_ids)} 处共享围挡" if network_changed else ""
    yield (
        "stage",
        {
            "stage": "isochrone",
            "message": (
                f"正在按真实路网采样，并用步行路线补回过街与路口等待{shared}…"
                if walking and refine_b.needs_routes
                else "正在按真实路网采样并拟合等时圈…"
            ),
        },
    )
    cfg = IsochroneConfig(minutes=req.minutes, directions=req.directions, mode_id=mode.id)
    result_a = await compute_isochrone(client, center, cfg, refine)
    # 叠加围挡的那一遍：采样点与路线都命中缓存，只是按新的围挡重新截断射线
    result = await compute_isochrone(client, center, cfg, refine_b) if network_changed else result_a
    payload = result.to_geojson()
    props = payload["properties"]
    # 地图中心必须用转换后的 BD09 坐标，否则前端以原始坐标为中心会整体偏移
    props["center"] = {"lat": round(center[0], 6), "lng": round(center[1], 6)}
    props["input_coord_sys"] = req.coord_sys
    if req.name and req.name.strip():
        props["name"] = req.name.strip()
    # 只记本次请求里用户自己画的临时围挡；共享围挡在 markings 里单独列出
    props["closures"] = [c.as_dict() for c in closures]
    if not walking and refine.needs_routes:
        props["refine_skipped"] = "过街等待校正与施工围挡只对步行等时圈计算。"
    isochrone_warnings(log, props)
    if mode.uses_traffic:
        props["traffic"] = traffic_summary(
            log,
            client.traffic_ages[traffic_mark[0] :],
            client.traffic_stale[traffic_mark[1] :],
            client.traffic_stale_reason,
            get_settings().traffic_ttl_s,
        )
    props["warnings"] = log.as_list()
    state.finish("等时圈")
    yield "isochrone", {"feature": payload}

    feature_a = result_a.to_geojson() if network_changed else payload
    props_a = feature_a["properties"]
    props_a["center"] = props["center"]

    def attach_markings(
        coverage_a: dict | None,
        coverage_b: dict | None,
        blind_a: dict | None,
        blind_b: dict | None,
    ) -> None:
        changed = network_changed or coverage_a is not coverage_b or blind_a is not blind_b
        report_a = build_report(props_a, coverage_a, blind_a) if changed else None
        marking_apply.compute_effects(
            mplan,
            rays=props.get("rays") or [],
            blind_a=blind_a,
            blind_b=blind_b,
            coverage_a=coverage_a,
            coverage_b=coverage_b,
        )
        props["markings"] = marking_apply.output(
            mplan,
            query_radius_m=query_radius,
            report_a=report_a,
            report_b=props["report"] if changed else None,
            props_a=props_a if changed else None,
            props_b=props,
            ring_a=feature_a["geometry"]["coordinates"][0] if network_changed else None,
            blind_a=blind_a,
            blind_b=blind_b,
            coverage_a=coverage_a,
            coverage_b=coverage_b,
        )

    if not req.coverage:
        props["report"] = build_report(props)
        attach_markings(None, None, None, None)
        return

    state.enter("coverage")
    yield "stage", {"stage": "coverage", "message": "正在检索周边民生设施…"}
    # 检索半径要罩住整个网格范围，再加上 1 公里判定阈值：
    # 网格边上的居民走 900 米就能到网格外的药店，漏采会凭空造出盲区
    radius = int(max(result.max_radius_m, blind_cfg.extent_m) + blind_cfg.walk_limit_m)
    coverage = await collect_coverage(client, center, radius)
    coverage_b = (
        marking_apply.modify_coverage(coverage, mplan)
        if mplan.of_type("facility_missing", "facility_extra")
        else coverage
    )
    facility_changed = coverage_b is not coverage
    coverage_dict = coverage_b.as_dict(result.polygon)
    coverage_dict["source"] = f"实时采集，检索半径 {radius} 米"
    if network_changed or facility_changed:
        coverage_dict_a = coverage.as_dict(result_a.polygon)
        coverage_dict_a["source"] = coverage_dict["source"]
    else:
        coverage_dict_a = coverage_dict
    props["coverage"] = coverage_dict
    coverage_warnings(log, coverage.failed)
    props["warnings"] = log.as_list()
    props["report"] = build_report(props, coverage_dict, None)
    state.finish("设施检索")
    yield "coverage", {"feature": payload}

    blind_dict_a: dict | None = None
    if req.blindspots and mode.allow_grid_blindspots:
        state.enter("blindspots")
        yield (
            "stage",
            {
                "stage": "blindspots",
                "message": "正在逐格实测步行 1 公里能否到达菜场、药店、小学（计算量最大的一段）…",
            },
        )
        blind_a = await identify_blindspots(
            client,
            center,
            result_a.polygon,
            coverage,
            blind_cfg,
            refine=result_a.refine,
            closures=closures,
        )
        blind_dict_a = blind_a.as_dict()
        if network_changed or facility_changed:
            state.enter("markings")
            yield (
                "stage",
                {
                    "stage": "markings",
                    "message": (
                        f"正在叠加 {len(mplan.applied)} 条用户标注重新判定"
                        "（已测过的点对直接复用，只为新的候选测距）…"
                    ),
                },
            )
            blind_b = await identify_blindspots(
                client,
                center,
                result.polygon,
                coverage_b,
                blind_cfg,
                refine=result.refine,
                closures=merged,
            )
            props["blindspots"] = blind_b.as_dict()
        else:
            props["blindspots"] = blind_dict_a
        blindspot_warnings(log, props["blindspots"])
        props["warnings"] = log.as_list()
        props["report"] = build_report(props, coverage_dict, props["blindspots"])
        state.finish("网格盲区")
        yield "blindspots", {"feature": payload}
    elif req.blindspots:
        props["blindspots_skipped"] = (
            "网格盲区只对步行等时圈计算：判定口径是居民点步行 1 公里能否到达设施。"
            "骑行/驾车圈面积大一个数量级，铺同样网格会把算路配额打爆。"
        )

    state.enter("report")
    props["report"] = build_report(props, coverage_dict, props.get("blindspots"))
    attach_markings(coverage_dict_a, coverage_dict, blind_dict_a, props.get("blindspots"))


def _timeout_detail(settings_timeout: float, state: AnalysisState | None = None) -> dict:
    stage_label = STAGE_LABELS.get(state.stage, "") if state else ""
    kept = (
        f"已完成的{'、'.join(state.completed)}保留在地图上。" if state and state.completed else ""
    )
    return {
        "code": "analysis_timeout",
        "stage": state.stage if state else None,
        "stage_label": stage_label or None,
        "message": (
            f"分析超过 {settings_timeout:.0f} 秒已终止"
            + (f"，停在「{stage_label}」这一步" if stage_label else "")
            + f"。{kept}已测过的点对都已缓存，重新计算会从断点附近接着算、不重复耗配额；"
            "也可以降低方向数、关闭盲区判定，或调大 BAIDU_ANALYSIS_TIMEOUT。"
        ),
    }


def _sse(event: str, data: dict) -> str:
    """格式化一个 SSE 事件块（event + data + 空行结尾）。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/api/health", summary="健康检查")
async def health() -> dict:
    """进程存活即 ok；配置问题（缺 AK）与今天已耗尽的接口作为 warnings 列出，
    便于部署后一眼看出「为什么实时计算不能用」。"""
    client: BaiduMapClient = app.state.baidu
    warnings = _config_warnings()
    active_quota = {
        e["endpoint"]: e for e in client.quota_events if client.exhausted(e["endpoint"]) is not None
    }
    return {
        "status": "ok" if not warnings else "degraded",
        "server_ak_configured": not any(w["code"] == "missing_server_ak" for w in warnings),
        "browser_ak_configured": not any(w["code"] == "missing_browser_ak" for w in warnings),
        "quota_exhausted_today": [
            {"service": e["service"], "status": e["status"], "day": e["day"]}
            for e in active_quota.values()
        ],
        "warnings": warnings,
    }


@app.get("/api/config", summary="前端运行时配置")
async def config() -> dict:
    """下发浏览器端 AK 等前端所需配置，避免把 Key 编译进静态产物。"""
    s = get_settings()
    blind = BlindspotConfig()
    warnings = _config_warnings()
    return {
        "browser_ak": s.browser_ak,
        "server_ak_configured": not any(w["code"] == "missing_server_ak" for w in warnings),
        "warnings": warnings,
        "default_minutes": 15,
        "default_directions": 36,
        # 品类词表由后端下发，前端图例与后端判定口径因此不会各写一份而对不上
        "categories": [{"name": c.name, "key_facility": c.key_facility} for c in CATEGORIES],
        "walk_limit_m": blind.walk_limit_m,
        "grid": {
            "spacing_m": blind.grid_spacing_m,
            "layout": blind.layout,
            "extent_m": blind.extent_m,
        },
        "closure_limits": {
            "max_count": MAX_CLOSURES,
            "min_radius_m": MIN_CLOSURE_RADIUS_M,
            "max_radius_m": MAX_CLOSURE_RADIUS_M,
        },
        "modes": [
            {
                **m.as_public(),
                "matrix_batch_pairs": min(s.matrix_batch_size, m.matrix_product_limit),
            }
            for m in MODES.values()
        ],
        "default_mode": "walk",
        # 只告诉前端 AI 二次核对能不能用，Agent Plan 的 Token 本身永不下发
        "agent_plan": {"configured": s.agent_plan_configured},
        # 标注的枚举、文案与上限；admin_enabled 只说明审核有没有开，口令不下发
        "markings": marking_service().config(),
        "trip": trip_config(s),
    }


@app.get("/api/samples", summary="列出预生成的样例快照")
async def list_samples() -> dict:
    return {"samples": [s.__dict__ for s in samples.list_samples()]}


def _current(props: dict) -> dict:
    """按现版口径整理一份结果：旧版圆形网格只留圈内格子，报告据此重算。零 API 消耗。"""
    coverage = props.get("coverage") or {}
    annotate_coverage(coverage)
    grid = props.get("blindspots")
    if (
        grid
        and grid.get("fresh_rule_version") != VERSION
        and any(
            coverage.get("fresh_breakdown", {}).get(status, 0) for status in ("pending", "excluded")
        )
    ):
        for cell in grid.get("cells", []):
            cell["missing"] = [c for c in cell.get("missing", []) if c != FRESH]
            if FRESH not in cell.setdefault("unknown", []):
                cell["unknown"].append(FRESH)
            cell.setdefault("unknown_reasons", {})[FRESH] = "fresh_legacy"
            cell.setdefault("nearest_m", {})[FRESH] = None
        grid.get("blind_ratio", {}).pop(FRESH, None)
        if FRESH not in grid.setdefault("incomplete_categories", []):
            grid["incomplete_categories"].append(FRESH)
        grid["blind_count"] = sum(bool(c.get("missing")) for c in grid.get("cells", []))
        coverage["fresh_needs_refresh"] = True
    props["blindspots"] = scope_to_circle(grid)
    props["report"] = build_report(props, props.get("coverage"), props.get("blindspots"))
    for place in (props.get("coverage") or {}).get("places", []):
        place["id"] = trip_place_id(place)
    return props


def _load_sample_or_404(sample_id: str) -> dict:
    payload = samples.load_sample(sample_id)
    if payload is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "样例不存在"})
    props = _current(payload.setdefault("properties", {}))
    # 旧版快照没有 quality 字段；由射线表推导，前端据此展示算法质量
    props["quality"] = _quality_from_rays(props)
    return payload


@app.get("/api/samples/{sample_id}", summary="读取样例快照")
async def get_sample(sample_id: str) -> dict:
    return _load_sample_or_404(sample_id)


@app.get("/api/samples/{sample_id}/export", summary="导出样例体检成果")
async def export_sample(
    sample_id: str,
    format: str = Query("zip", pattern="^(md|markdown|json|geojson|csv|zip)$"),
) -> Response:
    """把一次体检打包成可验收交付物，零 API 消耗。

    评审场景下「一份 Markdown 报告 + 数据文件」比截图更能体现工程完整度。
    """
    return _export_response(_load_sample_or_404(sample_id), format)


class ExportRequest(BaseModel):
    feature: dict = Field(..., description="要导出的完整 Feature（实时计算或历史记录的结果）")
    format: str = Field("zip", pattern="^(md|markdown|json|geojson|csv|zip)$")


@app.post("/api/export", summary="导出任意一次体检成果（实时 / 历史 / 样例）")
async def export_feature(req: ExportRequest) -> Response:
    """实时计算与历史记录也要能交付：报告按当前口径重算后打包，零 API 消耗。"""
    payload = req.feature
    props = payload.setdefault("properties", {})
    if not payload.get("geometry") or not props:
        raise HTTPException(
            status_code=400,
            detail={"code": "bad_feature", "message": "导出内容不是一次完整的体检结果。"},
        )
    _current(props)
    props["quality"] = _quality_from_rays(props)
    return _export_response(payload, req.format)


def _export_response(payload: dict, format: str) -> Response:
    if format in ("md", "markdown"):
        content: bytes | str = export.build_markdown(payload)
        media, ext = "text/markdown; charset=utf-8", "md"
    elif format == "geojson":
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        media, ext = "application/geo+json", "geojson"
    elif format == "csv":
        content = export.build_csv(payload)
        media, ext = "text/csv; charset=utf-8", "csv"
    elif format == "json":
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        media, ext = "application/json", "json"
    else:
        content = export.export_bundle(payload)
        media, ext = "application/zip", "zip"

    # RFC 5987 编码中文文件名，避免 Content-Disposition 乱码
    filename = quote(export.export_name(payload, ext))
    return Response(
        content=content,
        media_type=media,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


def _device_hash(device_id: str | None) -> str | None:
    """分析请求带上设备 ID，才知道哪些标注是「自己的」（自己的待核实标注对自己生效）。"""
    return marking_service().viewer(device_id, None).device_hash


@app.post("/api/isochrone", summary="实时计算步行等时圈与服务盲区")
async def isochrone(req: IsochroneRequest, x_device_id: str | None = Header(None)) -> dict:
    """基于真实路网计算等时圈，并按需叠加设施覆盖与网格盲区。

    三段的配额消耗量级完全不同，故各自可关：

    - 等时圈：directions × 7 个点对（步行 36 方向 252 个，冷缓存 6 次请求），
      步行时另有每方向 1 次步行路线规划，用于补回过街等待与围挡判定；
    - 覆盖层：各品类关键词数之和约 17 次起的地点检索（日额度 3000）；
    - 盲区层：15 分钟圈内 100 米网格（样例 116~144 格），热力每格 1 个点对 + 逐格判定数百个点对。

    命中缓存时均为 0 次。整个分析有总时长上限（analysis_timeout_s），超时立即终止。
    """
    client: BaiduMapClient = app.state.baidu
    mode = _get_mode_or_400(req.mode)
    settings = get_settings()
    usage_before = client.usage_snapshot()
    center = await _resolve_center(client, req)

    payload: dict | None = None
    state = AnalysisState()
    device_hash = _device_hash(x_device_id)
    try:
        async with asyncio.timeout(settings.analysis_timeout_s):
            async for _event, data in _analysis_steps(
                client, req, mode, center, state, device_hash
            ):
                if "feature" in data:
                    payload = data["feature"]
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504, detail=_timeout_detail(settings.analysis_timeout_s, state)
        ) from exc
    except BaiduApiError as exc:
        raise _handle_baidu_error(exc, state.stage, state.completed) from exc

    assert payload is not None
    props = payload["properties"]
    props["quota"] = _quota_delta(usage_before, client.usage_snapshot(), props.get("blindspots"))
    props["history_id"] = storage.save_analysis(req.model_dump(), payload)
    return payload


@app.post("/api/isochrone/stream", summary="实时计算（SSE 渐进式输出）")
async def isochrone_stream(
    req: IsochroneRequest, x_device_id: str | None = Header(None)
) -> StreamingResponse:
    """与 /api/isochrone 相同的计算，但以 SSE 分阶段推送结果：

    stage（阶段提示）→ isochrone（等时圈先上图）→ coverage（设施与初步报告）
    → blindspots（网格盲区）→ done（完整报告 + 配额 + 历史号）。

    完整分析在矩阵串行 + 令牌桶限速下可达一分钟上下，渐进输出让用户
    第一眼就能看到圈，而不是盯着遮罩等待。错误经 error 事件传递
    （响应头已发出，无法再改状态码）。
    """
    client: BaiduMapClient = app.state.baidu
    mode = _get_mode_or_400(req.mode)
    device_hash = _device_hash(x_device_id)

    async def gen():  # type: ignore[no-untyped-def]
        settings = get_settings()
        payload: dict | None = None
        state = AnalysisState()
        try:
            usage_before = client.usage_snapshot()
            center = await _resolve_center(client, req)
            async with asyncio.timeout(settings.analysis_timeout_s):
                async for event, data in _analysis_steps(
                    client, req, mode, center, state, device_hash
                ):
                    if "feature" in data:
                        payload = data["feature"]
                    yield _sse(event, data)
            assert payload is not None
            props = payload["properties"]
            props["quota"] = _quota_delta(
                usage_before, client.usage_snapshot(), props.get("blindspots")
            )
            props["history_id"] = storage.save_analysis(req.model_dump(), payload)
            yield _sse("done", {"feature": payload})
        except TimeoutError:
            yield _sse("error", _timeout_detail(settings.analysis_timeout_s, state))
        except HTTPException as exc:
            detail = (
                exc.detail
                if isinstance(exc.detail, dict)
                else {"code": "error", "message": str(exc.detail)}
            )
            yield _sse("error", {**detail, "status": exc.status_code})
        except BaiduApiError as exc:
            http_exc = _handle_baidu_error(exc, state.stage, state.completed)
            detail = (
                http_exc.detail
                if isinstance(http_exc.detail, dict)
                else {"code": "error", "message": str(http_exc.detail)}
            )
            yield _sse("error", {**detail, "status": http_exc.status_code})
        except Exception as exc:  # noqa: BLE001
            yield _sse(
                "error",
                {
                    "code": "internal_error",
                    "message": f"分析失败：{type(exc).__name__}: {str(exc)[:160]}",
                },
            )

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 反向代理（nginx 等）需关闭缓冲，SSE 才能逐段到达
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/histories", summary="列出实时分析历史")
async def histories(limit: int = Query(20, ge=1, le=100)) -> dict:
    return {"items": storage.list_analyses(limit)}


@app.get("/api/histories/{analysis_id}", summary="读取一条历史分析")
async def history_detail(analysis_id: int) -> dict:
    record = storage.get_analysis(analysis_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": "历史记录不存在"},
        )
    result = record.get("result")
    if isinstance(result, dict) and result.get("properties"):
        _current(result["properties"])
    return record


@app.post("/api/simulate", summary="模拟新建设施（按路网核验被消去的盲区方格）")
async def simulate(req: SimulateRequest) -> dict:
    """「假如在这里新建一处设施」——演示规划建议的即时效果。

    与盲区判定同一口径：步行 1 公里。先用直线下界挑出可能被覆盖的方格，
    再对它们做一次「多起点 × 拟建点」的批量算路（通常不超过 100 个点对）。
    离线模拟数据或 verify=false 时只做直线估算，结果标注为上限。
    """
    client: BaiduMapClient = app.state.baidu
    feature = req.feature
    props = feature.get("properties")
    if isinstance(props, dict):
        # 旧版结果也只在圈内找候选方格，与地图上的灰色区域一致
        props["blindspots"] = scope_to_circle(props.get("blindspots"))
    try:
        return await simulate_facility(
            client, feature, req.category, req.lat, req.lng, verify=req.verify
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "no_grid", "message": str(exc)}
        ) from exc
    except BaiduApiError as exc:
        raise _handle_baidu_error(exc, "simulate") from exc


@app.post("/api/simulate/closures", summary="假设道路封闭：预览体检结果，不保存正式历史")
async def preview_closures(
    req: ClosurePreviewRequest, x_device_id: str | None = Header(None)
) -> dict:
    props = req.feature.get("properties") or {}
    center = props.get("center") or {}
    if "lat" not in center or "lng" not in center:
        raise HTTPException(
            400, detail={"code": "bad_feature", "message": "预览需要完整分析中心。"}
        )
    if props.get("mode", "walk") != "walk":
        raise HTTPException(
            400, detail={"code": "not_walking", "message": "道路封闭评估只支持步行分析。"}
        )
    if props.get("simulated"):
        raise HTTPException(
            400, detail={"code": "no_network", "message": "道路封闭评估需要真实路网结果。"}
        )
    _require_server_ak()
    grid = props.get("blindspots") or {}
    request = IsochroneRequest(
        lat=center.get("lat"),
        lng=center.get("lng"),
        minutes=props.get("minutes", 15),
        directions=max(8, min(72, len(props.get("rays") or []) or 36)),
        grid_spacing_m=grid.get("grid_spacing_m", 100),
        grid_extent_m=max(500, min(2000, grid.get("extent_m") or 1500)),
        closures=req.closures,
        mode="walk",
        crossing_delay=True,
        markings=MarkingOptions(
            mode="auto", include=[m["id"] for m in (props.get("markings") or {}).get("applied", [])]
        ),
    )
    state = AnalysisState()
    payload = None
    baseline = None
    try:
        async with asyncio.timeout(get_settings().analysis_timeout_s):
            # 旧快照与当前规则/设施证据不同，直接作比较会把数据更新误算成封路效果。
            # 两遍使用相同参数与正常缓存；基线保留有效共享围挡，只移除本次假设围挡。
            before_request = request.model_copy(update={"closures": []})
            for analysis_request in (before_request, request) if req.closures else (request,):
                current = None
                async for _, data in _analysis_steps(
                    app.state.baidu,
                    analysis_request,
                    WALK,
                    (request.lat, request.lng),
                    state,
                    _device_hash(x_device_id),
                ):
                    if "feature" in data:
                        current = data["feature"]
                if baseline is None:
                    baseline = current
                payload = current
    except BaiduApiError as exc:
        raise _handle_baidu_error(exc, "simulate") from exc
    except TimeoutError as exc:
        raise HTTPException(
            504, detail=_timeout_detail(get_settings().analysis_timeout_s, state)
        ) from exc
    if payload is None:
        raise HTTPException(503, detail={"code": "no_preview", "message": "未生成道路封闭预览。"})
    payload["properties"]["planning_preview"] = True
    baseline_props = (baseline or {}).get("properties") or {}
    payload["properties"]["planning_baseline"] = {
        "report": baseline_props.get("report"),
        "area_km2": baseline_props.get("area_km2", 0),
    }
    return payload


def _require_server_ak() -> None:
    if any(w["code"] == "missing_server_ak" for w in _config_warnings()):
        raise HTTPException(
            status_code=503, detail={"code": "missing_server_ak", "message": MISSING_AK_MESSAGE}
        )


def _prepared(feature: dict) -> dict:
    """按当前口径重算报告：前端传回的结果可能来自旧版本，处方与灰色区域以后端为准。"""
    props = feature.setdefault("properties", {})
    if not feature.get("geometry") or not props:
        raise HTTPException(
            status_code=400,
            detail={"code": "bad_feature", "message": "请求里不是一次完整的体检结果。"},
        )
    _current(props)
    return feature


install_trip(app, _prepared, _require_server_ak, _handle_baidu_error)


@app.post("/api/recheck", summary="复测巡检：重取各方向步行路线，找疑似新增阻断")
async def recheck(req: FeatureRequest) -> dict:
    """跳过缓存重新规划这份结果里各方向的步行路线（约 36 次路线规划，不占批量算路点对），
    与结果里保存的路线比较；明显变长、且能定位被放弃路段的，标为「疑似新增阻断」。"""
    feature = _prepared(req.feature)
    try:
        baselines(feature)
    except NoBaselineError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "no_route_baseline", "message": str(exc)}
        ) from exc
    _require_server_ak()
    client: BaiduMapClient = app.state.baidu
    try:
        result = await recheck_routes(client, feature)
    except BaiduApiError as exc:
        raise _handle_baidu_error(exc, "recheck") from exc
    if result["checked"] and result["failed"] == result["checked"]:
        quota = client.exhausted("/directionlite/v1/walking")
        reason = quota_hint(quota) if quota else "；".join(result["route_failures"]) or "原因未知"
        raise HTTPException(
            status_code=503 if quota else 502,
            detail={
                "code": "recheck_failed",
                "service": "步行路线规划",
                "message": f"复测的 {result['checked']} 条步行路线全部没取到：{reason}。",
            },
        )
    return result


@app.post("/api/construction-candidates", summary="工地 POI 候选（施工围挡线索）")
async def construction_candidates(req: ConstructionRequest) -> dict:
    """按「工地」「施工」各检索一页（共 2 次地点检索，缓存 30 天），筛出疑似施工现场。
    只是候选：需要用户确认后才作为施工围挡参与计算。"""
    _require_server_ak()
    client: BaiduMapClient = app.state.baidu
    try:
        result = await find_construction(client, (req.lat, req.lng), req.radius_m, req.feature)
    except BaiduApiError as exc:
        raise _handle_baidu_error(exc, "construction") from exc
    if len(result["failed_keywords"]) == len(result["keywords"]):
        quota = client.exhausted("/place/v2/search")
        raise HTTPException(
            status_code=503 if quota else 502,
            detail={
                "code": "poi_failed",
                "service": "地点检索",
                "message": (
                    f"{quota_hint(quota)}。" if quota else "工地关键词检索全部失败，请稍后重试。"
                ),
            },
        )
    return result


@app.post("/api/site-plan", summary="核验选址：备选点位路网实测后重排")
async def site_plan(req: SitePlanRequest) -> dict:
    """对某一品类的补设处方取前几名备选点（相隔至少 300 米），逐个做路网核验
    （圈内 100 米网格上每个约百个点对，总量受 budget_pairs 限制），按实测效果重排并给出地址。"""
    feature = _prepared(req.feature)
    simulated = bool(feature["properties"].get("simulated"))
    if not simulated:
        _require_server_ak()
    client: BaiduMapClient = app.state.baidu
    before = client.usage_snapshot()
    try:
        result = await plan_site(
            None if simulated else client,
            feature,
            req.category,
            alternatives=req.alternatives,
            budget_pairs=req.budget_pairs,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "no_site", "message": str(exc)}
        ) from exc
    except BaiduApiError as exc:
        raise _handle_baidu_error(exc, "site_plan") from exc
    after = client.usage_snapshot()
    result["quota"] = {
        "matrix_pairs": after["matrix_pairs"] - before["matrix_pairs"],
        "regeo_queries": after["requests"].get("regeo", 0) - before["requests"].get("regeo", 0),
    }
    return result


@app.post("/api/narrate", summary="生成综合结论（按模板由算法结果生成，零 API 消耗）")
async def narrate_feature(req: NarrateRequest) -> dict:
    """把体检结果写成四段话：总体结论、主要问题、诊疗建议、数据边界。数字全部取自算法结果。"""
    feature = _prepared(req.feature)
    return narrate(feature)


@app.post(
    "/api/crosscheck",
    summary="AI 二次核对：用百度地图 Agent Plan 再找一遍灰色区域附近的关键设施",
)
async def crosscheck_feature(req: CrosscheckRequest) -> dict:
    """关键词检索漏掉设施会凭空造出盲区。对每片灰色区域、每个缺的关键品类，用 Agent Plan 的
    语义地点检索问一次「附近最近的 X」，和已收录的设施逐个比对，列出疑似漏收录的设施。

    只给线索，不改结论：用户可以把疑似设施按补录模拟一次，或核实后共享为「补录设施」标注。
    同样的问题只问一次（回答缓存 30 天）；每次最多问 MAX_QUESTIONS 个问题。
    """
    settings = get_settings()
    if not settings.agent_plan_configured:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "agent_plan_not_configured",
                "message": "没有配置百度地图 Agent Plan 的 Token（BAIDU_MAP_AUTH_TOKEN），"
                "AI 二次核对不可用。",
            },
        )
    feature = _prepared(req.feature)
    props = feature["properties"]
    if props.get("simulated"):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "simulated",
                "message": "离线模拟结果里的设施是伪随机生成的，不能拿真实地图核对。",
            },
        )
    center = props.get("center") or {}
    if "lat" not in center or "lng" not in center:
        raise HTTPException(
            status_code=400,
            detail={"code": "no_center", "message": "结果里没有中心点，无法确定检索范围。"},
        )
    try:
        crosscheck_quota.take()
    except crosscheck.RateLimited as exc:
        raise HTTPException(
            status_code=429, detail={"code": "agent_plan_rate_limited", "message": str(exc)}
        ) from exc

    _require_server_ak()  # 用逆地理编码确定所在城市与区县（Agent Plan 的检索要求带上）
    client: BaiduMapClient = app.state.baidu
    place = await client.reverse_geocode(float(center["lat"]), float(center["lng"]))
    region_name = crosscheck.region_from_geocode(place)
    if not region_name:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "region_unknown",
                "message": "逆地理编码没有返回所在城市与区县，暂时无法向 Agent Plan 提问，"
                "请稍后再试。",
            },
        )
    async with crosscheck.AgentPlanClient(settings.agent_plan_token, settings.cache_dir) as ap:
        result = await crosscheck.crosscheck(feature, ap, region_name, only_region=req.region)
        crosscheck_quota.spend(ap.requests)
        return crosscheck.as_payload(result, region_name, ap)


@app.get("/api/demo", summary="离线模拟分析（零 API 消耗）")
async def demo(
    lat: float = Query(..., ge=-90, le=90, description="中心点纬度"),
    lng: float = Query(..., ge=-180, le=180, description="中心点经度"),
    minutes: float = Query(15.0, gt=0, le=60),
    directions: int = Query(36, ge=8, le=72),
    mode: str = Query("walk"),
) -> dict:
    """确定性伪随机生成模拟体检结果，AK 失效时的保底演示路径。

    同一中心点重复请求结果完全一致；输出标注 simulated，不冒充真实数据。
    """
    _get_mode_or_400(mode)
    return build_demo_feature(lat, lng, minutes, directions, mode)


@app.get("/api/geocode", summary="地址转坐标")
async def geocode(address: str = Query(..., min_length=2, max_length=120)) -> dict:
    client: BaiduMapClient = app.state.baidu
    try:
        lat, lng = await client.geocode(address)
    except BaiduApiError as exc:
        raise _handle_baidu_error(exc, "geocode") from exc
    return {"address": address, "lat": lat, "lng": lng}
