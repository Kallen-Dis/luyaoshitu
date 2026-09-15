"""FastAPI 应用入口。

对外暴露等时圈计算与示例数据两条路径。默认走示例数据，实时计算需显式请求：
快照零 API 消耗，首屏不受配额与网络波动影响。

浏览器端 AK 通过 /api/config 下发而非编译进前端产物，这样换 Key 无需重新构建，
也便于在部署时通过环境变量覆盖。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import samples
from .baidu.client import BaiduMapClient
from .baidu.errors import BaiduApiError, ConfigurationError, QuotaExhaustedError
from .config import get_settings
from .isochrone.algorithm import IsochroneConfig, compute_isochrone
from .poi.catalog import CATEGORIES
from .poi.collect import collect_coverage
from .report.blindspot import BlindspotConfig, identify_blindspots
from .report.score import build_report


class IsochroneRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90, description="中心点纬度（BD09）")
    lng: float = Field(..., ge=-180, le=180, description="中心点经度（BD09）")
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


class GeocodeRequest(BaseModel):
    address: str = Field(..., min_length=2, max_length=120)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """进程级共享百度客户端。

    连接池复用能省下每次请求的 TLS 握手（实测约 1 秒），
    令牌桶也必须是进程唯一的，否则多个客户端各自限速会突破总配额。
    """
    async with BaiduMapClient() as client:
        app.state.baidu = client
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
    props["report"] = build_report(props, props.get("coverage"), props.get("blindspots"))
    return payload


@app.post("/api/isochrone", summary="实时计算步行等时圈与服务盲区")
async def isochrone(req: IsochroneRequest) -> dict:
    """基于真实路网计算等时圈，并按需叠加设施覆盖与网格盲区。

    三段的配额消耗量级完全不同，故各自可关：

    - 等时圈：directions × 7 ÷ 100 次批量算路（36 方向约 3 次）；
    - 覆盖层：各品类关键词数之和约 13 次地点检索（日额度 3000）；
    - 盲区层：网格数 × 关键品类候选数 ÷ 100 次批量算路（约 20~30 次）。

    命中缓存时均为 0 次。覆盖层失败不影响等时圈返回——降级的粒度就是这三段。
    """
    client: BaiduMapClient = app.state.baidu
    center = (req.lat, req.lng)
    cfg = IsochroneConfig(minutes=req.minutes, directions=req.directions)
    try:
        result = await compute_isochrone(client, center, cfg)
    except BaiduApiError as exc:
        raise _handle_baidu_error(exc) from exc

    payload = result.to_geojson()
    props = payload["properties"]
    blind_cfg = BlindspotConfig(grid_spacing_m=req.grid_spacing_m)

    coverage_dict: dict | None = None
    blind_dict: dict | None = None
    if req.coverage:
        # 采集半径要把等时圈整个罩住，再加上盲区判定的 1 公里阈值：
        # 圈外 900 米的药店对圈边居民依然有效，漏采会凭空造出盲区
        radius = int(result.max_radius_m + blind_cfg.walk_limit_m)
        try:
            coverage = await collect_coverage(client, center, radius)
        except BaiduApiError as exc:
            raise _handle_baidu_error(exc) from exc
        coverage_dict = coverage.as_dict(result.polygon)
        coverage_dict["source"] = f"实时采集，检索半径 {radius} 米"
        props["coverage"] = coverage_dict

        if req.blindspots:
            try:
                blind = await identify_blindspots(
                    client, center, result.polygon, coverage, blind_cfg
                )
            except BaiduApiError as exc:
                raise _handle_baidu_error(exc) from exc
            blind_dict = blind.as_dict()
            props["blindspots"] = blind_dict

    props["report"] = build_report(props, coverage_dict, blind_dict)
    return payload


@app.get("/api/geocode", summary="地址转坐标")
async def geocode(address: str = Query(..., min_length=2, max_length=120)) -> dict:
    client: BaiduMapClient = app.state.baidu
    try:
        lat, lng = await client.geocode(address)
    except BaiduApiError as exc:
        raise _handle_baidu_error(exc) from exc
    return {"address": address, "lat": lat, "lng": lng}
