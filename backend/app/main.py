"""FastAPI 应用入口。

对外暴露等时圈计算与示例数据两条路径。默认走示例数据，实时计算需显式请求：
快照零 API 消耗，首屏不受配额与网络波动影响。

浏览器端 AK 通过 /api/config 下发而非编译进前端产物，这样换 Key 无需重新构建，
也便于在部署时通过环境变量覆盖。
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import export, samples, storage
from .baidu.client import BaiduMapClient
from .baidu.errors import BaiduApiError, ConfigurationError, QuotaExhaustedError
from .config import get_settings
from .demo import build_demo_feature
from .isochrone.algorithm import IsochroneConfig, compute_isochrone
from .poi.catalog import CATEGORIES
from .poi.collect import collect_coverage
from .report.blindspot import BlindspotConfig, identify_blindspots
from .report.score import build_report
from .report.wide_grid import polygon_of
from .travel import MODES, get_mode


class IsochroneRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90, description="中心点纬度")
    lng: float = Field(..., ge=-180, le=180, description="中心点经度")
    coord_sys: str = Field(
        "bd09",
        description="输入坐标系：bd09（百度）/ gcj02（高德、腾讯）/ wgs84（GPS）",
    )
    minutes: float = Field(15.0, gt=0, le=60, description="时间阈值（分钟）")
    directions: int = Field(36, ge=8, le=72, description="射线方向数，越多越精细")
    coverage: bool = Field(
        True, description="是否采集圈内民生设施覆盖（消耗地点检索配额）"
    )
    blindspots: bool = Field(
        True, description="是否做网格级盲区判定（消耗批量算路，且依赖 coverage）"
    )
    grid_spacing_m: float = Field(
        150.0, ge=80, le=400, description="盲区判定的网格间距，越小越精细也越耗配额"
    )
    mode: str = Field(
        "walk",
        description="出行方式：walk / ride / drive / drive_traffic",
    )


class GeocodeRequest(BaseModel):
    address: str = Field(..., min_length=2, max_length=120)


class SimulateRequest(BaseModel):
    """模拟新建：在指定位置放一处设施，本地重算盲区与分数（零 API）。"""

    category: str = Field(..., min_length=1, max_length=20, description="拟建设施品类")
    lat: float = Field(..., ge=-90, le=90)
    lng: float = Field(..., ge=-180, le=180)
    feature: dict = Field(..., description="当前分析结果的完整 Feature")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """进程级共享百度客户端。

    连接池复用能省下每次请求的 TLS 握手（实测约 1 秒），
    令牌桶也必须是进程唯一的，否则多个客户端各自限速会突破总配额。
    """
    async with BaiduMapClient() as client:
        app.state.baidu = client
        storage.init_db()
        yield


app = FastAPI(
    title="路遥识途——基于百度地图的生活圈智能规划",
    description="基于百度地图开放能力，计算真实路网步行等时圈并诊断民生设施服务盲区",
    version="0.1.0",
    lifespan=lifespan,
)

# 开发期允许 Vite 开发服务器跨域访问；生产部署时前端与后端同源，可收紧此配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def _handle_baidu_error(exc: BaiduApiError) -> HTTPException:
    """把百度的错误分类翻译成合适的 HTTP 状态与可读提示。

    三类错误对用户意味着完全不同的事情：配额耗尽要等次日，配置错误要改部署，
    其余才是可重试的临时故障。笼统地返回 500 会让使用者无从判断。
    """
    if isinstance(exc, QuotaExhaustedError):
        return HTTPException(
            status_code=429,
            detail={
                "code": "quota_exhausted",
                "message": "百度地图接口当日配额已耗尽，次日 0 点重置。请改用示例数据演示。",
                "status": exc.status,
            },
        )
    if isinstance(exc, ConfigurationError):
        return HTTPException(
            status_code=500,
            detail={
                "code": "configuration_error",
                "message": "AK 无效或白名单未配置，请检查 .env 与百度控制台设置。",
                "status": exc.status,
            },
        )
    return HTTPException(
        status_code=502,
        detail={"code": "upstream_error", "message": str(exc), "status": exc.status},
    )


def _quota_delta(
    before: dict, after: dict, blind: dict | None
) -> dict[str, int | None]:
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
    rays = props.get("rays") or []
    return {
        "directions": len(rays),
        "saturated": None,
        "barrier_truncated": sum(1 for r in rays if r.get("barrier")),
        "zero_radius": sum(1 for r in rays if (r.get("radius_m") or 0) <= 0),
    }


async def _resolve_center(
    client: BaiduMapClient, req: "IsochroneRequest"
) -> tuple[float, float]:
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
        raise _handle_baidu_error(exc) from exc
    if not converted:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "upstream_error",
                "message": "坐标转换返回为空，请稍后重试或改用 BD09 坐标输入。",
            },
        )
    return converted[0]


def _sse(event: str, data: dict) -> str:
    """格式化一个 SSE 事件块（event + data + 空行结尾）。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/api/health", summary="健康检查")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/api/config", summary="前端运行时配置")
async def config() -> dict:
    """下发浏览器端 AK 等前端所需配置，避免把 Key 编译进静态产物。"""
    s = get_settings()
    return {
        "browser_ak": s.browser_ak,
        "default_minutes": 15,
        "default_directions": 36,
        # 品类词表由后端下发，前端图例与后端判定口径因此不会各写一份而对不上
        "categories": [
            {"name": c.name, "key_facility": c.key_facility} for c in CATEGORIES
        ],
        "walk_limit_m": BlindspotConfig().walk_limit_m,
        "modes": [m.as_public() for m in MODES.values()],
        "default_mode": "walk",
    }


@app.get("/api/samples", summary="列出预生成的样例快照")
async def list_samples() -> dict:
    return {"samples": [s.__dict__ for s in samples.list_samples()]}


@app.get("/api/samples/{sample_id}", summary="读取样例快照")
async def get_sample(sample_id: str) -> dict:
    payload = samples.load_sample(sample_id)
    if payload is None:
        raise HTTPException(status_code=404, detail={"code": "not_found", "message": "样例不存在"})
    props = payload.setdefault("properties", {})
    props["report"] = build_report(
        props, props.get("coverage"), props.get("blindspots"), polygon_of(payload)
    )
    # 快照生成于旧版本，没有 quality 字段；由射线表推导，前端据此展示算法质量
    props["quality"] = _quality_from_rays(props)
    return payload


@app.get("/api/samples/{sample_id}/export", summary="导出样例体检成果")
async def export_sample(
    sample_id: str,
    format: str = Query("zip", pattern="^(md|markdown|json|geojson|csv|zip)$"),
) -> Response:
    """把一次体检打包成可验收交付物，零 API 消耗。

    评审场景下「一份 Markdown 报告 + 数据文件」比截图更能体现工程完整度。
    """
    payload = samples.load_sample(sample_id)
    if payload is None:
        raise HTTPException(
            status_code=404, detail={"code": "not_found", "message": "样例不存在"}
        )
    props = payload.setdefault("properties", {})
    props["report"] = build_report(
        props, props.get("coverage"), props.get("blindspots"), polygon_of(payload)
    )
    props["quality"] = _quality_from_rays(props)

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


@app.post("/api/isochrone", summary="实时计算步行等时圈与服务盲区")
async def isochrone(req: IsochroneRequest) -> dict:
    """基于真实路网计算等时圈，并按需叠加设施覆盖与网格盲区。

    三段的配额消耗量级完全不同，故各自可关：

    - 等时圈：directions × 7 ÷ 100 次批量算路（36 方向约 3 次）；
    - 覆盖层：各品类关键词数之和约 13 次地点检索（日额度 3000）；
    - 盲区层：网格数 × 关键品类候选数 ÷ 100 次批量算路（约 20~30 次）。

    命中缓存时均为 0 次。覆盖层失败不影响等时圈返回——降级的粒度就是这三段。

    整个分析有总时长上限（analysis_timeout_s）：矩阵串行 + 令牌桶限速下，
    完整分析通常几十秒；超时立即终止，避免上游抖动让请求无限挂起。
    """
    client: BaiduMapClient = app.state.baidu
    try:
        mode = get_mode(req.mode)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "bad_mode", "message": str(exc)},
        ) from exc
    usage_before = client.usage_snapshot()

    # 坐标转换：非 BD09 输入先统一到 BD09。geoconv 无日配额限制。
    center = await _resolve_center(client, req)
    center_sys = req.coord_sys

    settings = get_settings()

    async def _analyze() -> tuple[dict, dict | None, dict | None]:
        """分析主体：等时圈 → 覆盖 → 盲区。单独成函数以便整体加超时。"""
        cfg = IsochroneConfig(
            minutes=req.minutes, directions=req.directions, mode_id=mode.id
        )
        result = await compute_isochrone(client, center, cfg)

        payload = result.to_geojson()
        props = payload["properties"]
        # 地图中心必须用转换后的 BD09 坐标，否则前端以原始坐标为中心会整体偏移
        props["center"] = {"lat": round(center[0], 6), "lng": round(center[1], 6)}
        props["input_coord_sys"] = center_sys

        blind_cfg = BlindspotConfig(grid_spacing_m=req.grid_spacing_m)
        coverage_dict: dict | None = None
        blind_dict: dict | None = None
        if req.coverage:
            # 采集半径要把等时圈整个罩住，再加上盲区判定的 1 公里阈值：
            # 圈外 900 米的药店对圈边居民依然有效，漏采会凭空造出盲区
            radius = int(result.max_radius_m + blind_cfg.walk_limit_m)
            coverage = await collect_coverage(client, center, radius)
            coverage_dict = coverage.as_dict(result.polygon)
            coverage_dict["source"] = f"实时采集，检索半径 {radius} 米"
            props["coverage"] = coverage_dict

            if req.blindspots and mode.allow_grid_blindspots:
                blind = await identify_blindspots(
                    client, center, result.polygon, coverage, blind_cfg
                )
                blind_dict = blind.as_dict()
                props["blindspots"] = blind_dict
            elif req.blindspots:
                props["blindspots_skipped"] = (
                    "网格盲区只对步行等时圈计算：判定口径是居民点步行 1 公里能否到达设施。"
                    "骑行/驾车圈面积大一个数量级，铺同样网格会把算路配额打爆。"
                )

        props["report"] = build_report(
            props, coverage_dict, blind_dict, polygon_of(payload)
        )
        return payload, coverage_dict, blind_dict

    try:
        payload, coverage_dict, blind_dict = await asyncio.wait_for(
            _analyze(), timeout=settings.analysis_timeout_s
        )
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail={
                "code": "analysis_timeout",
                "message": (
                    f"分析超过 {settings.analysis_timeout_s:.0f} 秒已终止。"
                    "可降低方向数、关闭盲区判定，或改用左侧预生成样例。"
                ),
            },
        ) from exc
    except BaiduApiError as exc:
        raise _handle_baidu_error(exc) from exc

    payload["properties"]["quota"] = _quota_delta(
        usage_before, client.usage_snapshot(), blind_dict
    )
    history_id = storage.save_analysis(req.model_dump(), payload)
    payload["properties"]["history_id"] = history_id
    return payload


@app.post("/api/isochrone/stream", summary="实时计算（SSE 渐进式输出）")
async def isochrone_stream(req: IsochroneRequest) -> StreamingResponse:
    """与 /api/isochrone 相同的计算，但以 SSE 分阶段推送结果：

    stage（阶段提示）→ isochrone（等时圈先上图）→ coverage（设施与初步报告）
    → blindspots（网格盲区）→ done（完整报告 + 配额 + 历史号）。

    完整分析在矩阵串行 + 令牌桶限速下可达几十秒，渐进输出让用户
    第一眼就能看到圈，而不是盯着遮罩等待。错误经 error 事件传递
    （响应头已发出，无法再改状态码）。
    """
    client: BaiduMapClient = app.state.baidu
    try:
        mode = get_mode(req.mode)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "bad_mode", "message": str(exc)},
        ) from exc

    async def gen():  # type: ignore[no-untyped-def]
        settings = get_settings()
        deadline = time.monotonic() + settings.analysis_timeout_s

        def remaining() -> float:
            return max(1.0, deadline - time.monotonic())

        try:
            usage_before = client.usage_snapshot()
            center = await _resolve_center(client, req)
            center_sys = req.coord_sys

            yield _sse(
                "stage",
                {"stage": "isochrone", "message": "正在按真实路网采样并拟合等时圈…"},
            )
            cfg = IsochroneConfig(
                minutes=req.minutes, directions=req.directions, mode_id=mode.id
            )
            result = await asyncio.wait_for(
                compute_isochrone(client, center, cfg), timeout=remaining()
            )
            payload = result.to_geojson()
            props = payload["properties"]
            props["center"] = {"lat": round(center[0], 6), "lng": round(center[1], 6)}
            props["input_coord_sys"] = center_sys
            yield _sse("isochrone", {"feature": payload})

            coverage_dict: dict | None = None
            blind_dict: dict | None = None
            blind_cfg = BlindspotConfig(grid_spacing_m=req.grid_spacing_m)
            if req.coverage:
                yield _sse(
                    "stage",
                    {"stage": "coverage", "message": "正在检索周边民生设施…"},
                )
                radius = int(result.max_radius_m + blind_cfg.walk_limit_m)
                coverage = await asyncio.wait_for(
                    collect_coverage(client, center, radius), timeout=remaining()
                )
                coverage_dict = coverage.as_dict(result.polygon)
                coverage_dict["source"] = f"实时采集，检索半径 {radius} 米"
                props["coverage"] = coverage_dict
                props["report"] = build_report(
                    props, coverage_dict, None, polygon_of(payload)
                )
                yield _sse("coverage", {"feature": payload})

                if req.blindspots and mode.allow_grid_blindspots:
                    yield _sse(
                        "stage",
                        {
                            "stage": "blindspots",
                            "message": "正在做网格级盲区判定（计算量最大的一段）…",
                        },
                    )
                    blind = await asyncio.wait_for(
                        identify_blindspots(
                            client, center, result.polygon, coverage, blind_cfg
                        ),
                        timeout=remaining(),
                    )
                    blind_dict = blind.as_dict()
                    props["blindspots"] = blind_dict
                    props["report"] = build_report(
                        props, coverage_dict, blind_dict, polygon_of(payload)
                    )
                    yield _sse("blindspots", {"feature": payload})
                elif req.blindspots:
                    props["blindspots_skipped"] = (
                        "网格盲区只对步行等时圈计算：判定口径是居民点步行 1 公里能否到达设施。"
                        "骑行/驾车圈面积大一个数量级，铺同样网格会把算路配额打爆。"
                    )

            props["report"] = build_report(
                props, coverage_dict, blind_dict, polygon_of(payload)
            )
            props["quota"] = _quota_delta(
                usage_before, client.usage_snapshot(), blind_dict
            )
            history_id = storage.save_analysis(req.model_dump(), payload)
            props["history_id"] = history_id
            yield _sse("done", {"feature": payload})
        except asyncio.TimeoutError:
            yield _sse(
                "error",
                {
                    "code": "analysis_timeout",
                    "message": (
                        f"分析超过 {settings.analysis_timeout_s:.0f} 秒已终止。"
                        "可降低方向数、关闭盲区判定，或改用预生成样例。"
                    ),
                },
            )
        except HTTPException as exc:
            detail = (
                exc.detail
                if isinstance(exc.detail, dict)
                else {"code": "error", "message": str(exc.detail)}
            )
            yield _sse("error", {**detail, "status": exc.status_code})
        except BaiduApiError as exc:
            http_exc = _handle_baidu_error(exc)
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
    return record


@app.post("/api/simulate", summary="模拟新建设施（零 API 消耗）")
async def simulate(req: SimulateRequest) -> dict:
    """「假如在这里新建一处设施」——演示规划建议的即时效果。

    按直线距离 ≤ 步行阈值（1 公里）近似判定拟建设施能覆盖哪些盲区网格，
    重算盲区占比与体检分，返回前后对比。全部本地计算，零 API 消耗。

    近似口径须如实声明：直线是步行距离的下界，真实效果需设施建成后
    按路网复测；这里给的是规划阶段的量级判断，不是承诺值。
    """
    props = dict(req.feature.get("properties") or {})
    polygon = polygon_of(req.feature)
    coverage = props.get("coverage") or {}
    places = coverage.get("places") or []
    if not (polygon and places and props.get("center")):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "no_places",
                "message": "当前结果没有设施坐标，无法按地图上的方格模拟。",
            },
        )

    before_report = build_report(props, coverage, props.get("blindspots"), polygon)
    after_cov = copy.deepcopy(coverage)
    after_cov["places"] = [
        *places,
        {
            "category": req.category,
            "name": "拟建",
            "lat": req.lat,
            "lng": req.lng,
            "in_circle": True,
        },
    ]
    after_report = build_report(props, after_cov, props.get("blindspots"), polygon)
    before_n = int(before_report.get("blind_cell_count") or 0)
    after_n = int(after_report.get("blind_cell_count") or 0)

    return {
        "category": req.category,
        "lat": req.lat,
        "lng": req.lng,
        "covered_cells": [],
        "covered_count": max(0, before_n - after_n),
        "before": {
            "score": before_report.get("total"),
            "grade": before_report.get("grade"),
            "blind_count": before_report.get("blind_cell_count"),
            "blind_ratio": before_report.get("blind_ratio"),
        },
        "after": {
            "score": after_report.get("total"),
            "grade": after_report.get("grade"),
            "blind_count": after_report.get("blind_cell_count"),
            "blind_ratio": after_report.get("blind_ratio"),
        },
        "approximation": (
            "和地图用同一套方格：中心 1.5 公里、200 米一格，直线 1 公里内缺这一类的格子会消去。"
            "直线是步行距离的下界，建成后要按路网再测一次。"
        ),
    }


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
    try:
        get_mode(mode)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "bad_mode", "message": str(exc)},
        ) from exc
    return build_demo_feature(lat, lng, minutes, directions, mode)


@app.get("/api/geocode", summary="地址转坐标")
async def geocode(address: str = Query(..., min_length=2, max_length=120)) -> dict:
    client: BaiduMapClient = app.state.baidu
    try:
        lat, lng = await client.geocode(address)
    except BaiduApiError as exc:
        raise _handle_baidu_error(exc) from exc
    return {"address": address, "lat": lat, "lng": lng}
