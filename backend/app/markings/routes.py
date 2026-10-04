"""标注相关的 HTTP 接口。

写接口现阶段对任何人开放（只要带设备 ID）；修改类操作要出示新建时拿到的编辑令牌；
审核接口要管理员口令（X-Admin-Token），没配 ADMIN_TOKEN 时整组关闭。

照片上传直接收原始字节（Content-Type: image/*），不走 multipart：省掉一个依赖，
也方便边读边限长——超过上限立即 413，不会把几十 MB 读进内存。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, FastAPI, Header, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ..config import get_settings
from . import store as store_mod
from .photos import MAX_BYTES
from .service import MarkingError, MarkingService, Viewer

router = APIRouter(prefix="/api", tags=["markings"])

_service: MarkingService | None = None


def get_service() -> MarkingService:
    """进程内共享一个服务实例（限流与审核记录都在它里面）。数据目录变了就重建。"""
    global _service
    root = store_mod.data_root()
    if _service is None or _service.store.root != root:
        _service = MarkingService(store_mod.MarkingStore(root), get_settings())
    return _service


def _viewer(request: Request, device_id: str | None) -> Viewer:
    ip = request.client.host if request.client else "unknown"
    return get_service().viewer(device_id, ip)


class MarkingIn(BaseModel):
    """新建标注。字段的业务校验在 models.normalize 里做，这里只挡住类型与超长输入。"""

    type: str = Field(..., max_length=32)
    kind: str | None = Field(None, max_length=32)
    lat: float | None = None
    lng: float | None = None
    radius_m: float | None = None
    polygon: list[list[float]] | None = Field(None, max_length=80)
    category: str | None = Field(None, max_length=20)
    categories: list[str] | None = Field(None, max_length=10)
    name: str | None = Field(None, max_length=200)
    reason: str | None = Field(None, max_length=32)
    note: str | None = Field(None, max_length=400)
    expires_in_days: int | None = None
    source: str = Field("user", max_length=16)
    force: bool = False
    # 必填：先 POST /api/markings/uploads 传照片拿到的 ID，新建时在同一个事务里挂上
    photos: list[str] = Field(default_factory=list, max_length=6)


class MarkingPatch(BaseModel):
    version: int = Field(..., ge=1, description="修改基于的版本号（乐观并发）")
    kind: str | None = Field(None, max_length=32)
    lat: float | None = None
    lng: float | None = None
    radius_m: float | None = None
    polygon: list[list[float]] | None = Field(None, max_length=80)
    category: str | None = Field(None, max_length=20)
    categories: list[str] | None = Field(None, max_length=10)
    name: str | None = Field(None, max_length=200)
    reason: str | None = Field(None, max_length=32)
    note: str | None = Field(None, max_length=400)
    expires_in_days: int | None = None


class RevertIn(BaseModel):
    version: int = Field(..., ge=1)
    to_version: int = Field(..., ge=1)


class VoteIn(BaseModel):
    vote: int = Field(..., ge=-1, le=1)


class ReviewIn(BaseModel):
    decision: str = Field(..., max_length=16)
    version: int = Field(..., ge=1)
    note: str = Field("", max_length=600)
    basis: list[str] = Field(default_factory=list, max_length=4)
    checks: dict[str, bool] = Field(default_factory=dict)
    photos_reviewed: list[str] = Field(default_factory=list, max_length=20)
    reason: str | None = Field(None, max_length=32)
    expires_in_days: int | None = None


class PhotoVisibilityIn(BaseModel):
    hidden: bool
    note: str = Field(..., max_length=400)


def _photo_response(path: Any, mime: str, *, cache: str) -> FileResponse:
    return FileResponse(
        path,
        media_type=mime,
        headers={
            "Cache-Control": cache,
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cross-Origin-Resource-Policy": "same-origin",
        },
    )


# ---------- 公开与作者接口 ----------
# 注意顺序：/mine 与 /photos/... 必须写在 /{marking_id} 之前，否则会被当成 ID 去匹配


@router.get("/markings", summary="附近的有效标注（待核实 + 已核实）")
def nearby(
    request: Request,
    lat: float = Query(..., ge=-90, le=90),
    lng: float = Query(..., ge=-180, le=180),
    radius_m: float = Query(2500.0, gt=0, le=5000),
    types: str | None = Query(None, max_length=120, description="逗号分隔的类型"),
    x_device_id: str | None = Header(None),
) -> dict:
    wanted = tuple(t for t in (types or "").split(",") if t) or None
    svc = get_service()
    items = svc.nearby(lat, lng, radius_m, _viewer(request, x_device_id), wanted)
    return {"items": items, "radius_m": radius_m}


async def _read_image(request: Request) -> bytes:
    """边读边限长：声明或实际超过 4 MB 立即 413，不把整个请求体读进内存。"""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            too_big = int(declared) > MAX_BYTES
        except ValueError:
            too_big = False
        if too_big:
            raise MarkingError(413, "too_large", "图片超过 4 MB，请压缩后再上传")
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_BYTES:
            raise MarkingError(413, "too_large", "图片超过 4 MB，请压缩后再上传")
    return bytes(data)


@router.post(
    "/markings/uploads",
    summary="新建标注前先传现场照片（请求体为图片原始字节），返回预传 ID",
    status_code=201,
)
async def upload_before_create(request: Request, x_device_id: str | None = Header(None)) -> dict:
    data = await _read_image(request)
    viewer = _viewer(request, x_device_id)
    return await run_in_threadpool(get_service().stage_photo, data, viewer)


@router.post("/markings", summary="新建标注（至少附一张预传的现场照片）", status_code=201)
def create(request: Request, body: MarkingIn, x_device_id: str | None = Header(None)) -> dict:
    return get_service().create(body.model_dump(), _viewer(request, x_device_id))


@router.get("/markings/mine", summary="本设备提交的全部标注")
def mine(request: Request, x_device_id: str | None = Header(None)) -> dict:
    return {"items": get_service().mine(_viewer(request, x_device_id))}


@router.get("/markings/photos/{photo_id}", summary="标注照片")
def photo(photo_id: str) -> FileResponse:
    path, mime = get_service().photo_path(photo_id)
    # 照片 ID 是随机的、内容不变；缓存一天，隐藏或删除后最多一天内从各端消失
    return _photo_response(path, mime, cache="public, max-age=86400")


@router.delete(
    "/markings/photos/{photo_id}",
    summary="删除自己上传的照片",
    status_code=204,
    response_class=Response,
)
def delete_photo(
    request: Request,
    photo_id: str,
    x_device_id: str | None = Header(None),
    x_edit_token: str | None = Header(None),
) -> Response:
    get_service().delete_photo(photo_id, x_edit_token, _viewer(request, x_device_id))
    return Response(status_code=204)


@router.get("/markings/{marking_id}", summary="标注详情（照片、时间线、版本）")
def detail(request: Request, marking_id: int, x_device_id: str | None = Header(None)) -> dict:
    return get_service().detail(marking_id, _viewer(request, x_device_id))


@router.patch("/markings/{marking_id}", summary="修改标注（产生新版本）")
def update(
    request: Request,
    marking_id: int,
    body: MarkingPatch,
    x_device_id: str | None = Header(None),
    x_edit_token: str | None = Header(None),
) -> dict:
    patch = body.model_dump(exclude={"version"})
    return get_service().update(
        marking_id, patch, body.version, x_edit_token, _viewer(request, x_device_id)
    )


@router.delete("/markings/{marking_id}", summary="撤回标注（可恢复）")
def retract(
    request: Request,
    marking_id: int,
    x_device_id: str | None = Header(None),
    x_edit_token: str | None = Header(None),
) -> dict:
    return get_service().retract(marking_id, x_edit_token, _viewer(request, x_device_id))


@router.post("/markings/{marking_id}/restore", summary="恢复撤回的标注")
def restore(
    request: Request,
    marking_id: int,
    x_device_id: str | None = Header(None),
    x_edit_token: str | None = Header(None),
) -> dict:
    return get_service().restore(marking_id, x_edit_token, _viewer(request, x_device_id))


@router.post("/markings/{marking_id}/revert", summary="回到某个旧版本（产生新版本）")
def revert(
    request: Request,
    marking_id: int,
    body: RevertIn,
    x_device_id: str | None = Header(None),
    x_edit_token: str | None = Header(None),
) -> dict:
    return get_service().revert(
        marking_id, body.to_version, body.version, x_edit_token, _viewer(request, x_device_id)
    )


@router.put("/markings/{marking_id}/vote", summary="确认 / 有异议 / 撤票")
def vote(
    request: Request, marking_id: int, body: VoteIn, x_device_id: str | None = Header(None)
) -> dict:
    return get_service().vote(marking_id, body.vote, _viewer(request, x_device_id))


@router.post(
    "/markings/{marking_id}/photos", summary="上传照片（请求体为图片原始字节）", status_code=201
)
async def upload_photo(
    request: Request,
    marking_id: int,
    x_device_id: str | None = Header(None),
    x_edit_token: str | None = Header(None),
) -> dict:
    data = await _read_image(request)
    viewer = _viewer(request, x_device_id)
    return await run_in_threadpool(get_service().add_photo, marking_id, data, x_edit_token, viewer)


# ---------- 管理员 ----------


def _admin(request: Request, token: str | None, device_id: str | None) -> Viewer:
    viewer = _viewer(request, device_id)
    get_service().require_admin(token, viewer)
    return viewer


@router.get("/admin/markings", summary="审核队列")
def admin_queue(
    request: Request,
    status: str = Query("pending", max_length=16),
    x_admin_token: str | None = Header(None),
    x_device_id: str | None = Header(None),
) -> dict:
    viewer = _admin(request, x_admin_token, x_device_id)
    return get_service().admin_queue(status, viewer)


@router.get("/admin/markings/{marking_id}", summary="审核详情（打开后才能核实）")
def admin_detail(
    request: Request,
    marking_id: int,
    x_admin_token: str | None = Header(None),
    x_device_id: str | None = Header(None),
) -> dict:
    viewer = _admin(request, x_admin_token, x_device_id)
    return get_service().admin_detail(marking_id, viewer)


@router.post("/admin/markings/{marking_id}/review", summary="审核：核实 / 驳回 / 归档 / 重新打开")
def admin_review(
    request: Request,
    marking_id: int,
    body: ReviewIn,
    x_admin_token: str | None = Header(None),
    x_device_id: str | None = Header(None),
) -> dict:
    viewer = _admin(request, x_admin_token, x_device_id)
    return get_service().admin_review(marking_id, body.model_dump(), viewer)


@router.get("/admin/photos/{photo_id}", summary="管理员查看照片（含已隐藏的）")
def admin_photo(
    request: Request,
    photo_id: str,
    x_admin_token: str | None = Header(None),
    x_device_id: str | None = Header(None),
) -> FileResponse:
    _admin(request, x_admin_token, x_device_id)
    path, mime = get_service().photo_path(photo_id, admin=True)
    return _photo_response(path, mime, cache="private, no-store")


@router.post("/admin/photos/{photo_id}/visibility", summary="隐藏或恢复一张照片")
def admin_photo_visibility(
    request: Request,
    photo_id: str,
    body: PhotoVisibilityIn,
    x_admin_token: str | None = Header(None),
    x_device_id: str | None = Header(None),
) -> dict:
    viewer = _admin(request, x_admin_token, x_device_id)
    return get_service().set_photo_visibility(photo_id, body.hidden, body.note, viewer)


def install(app: FastAPI) -> None:
    """挂上路由与错误处理。MarkingError 原样变成 {detail: {code, message, ...}}。"""

    async def handle(_request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, MarkingError)
        headers = {}
        if exc.status == 429 and exc.extra.get("retry_after_s"):
            headers["Retry-After"] = str(exc.extra["retry_after_s"])
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.detail()}, headers=headers
        )

    app.add_exception_handler(MarkingError, handle)
    app.include_router(router)


async def markings_for_analysis(
    lat: float, lng: float, radius_m: float, device_hash: str | None
) -> list[dict[str, Any]]:
    """分析流水线用：在线程里查库，不阻塞事件循环。"""
    return await asyncio.to_thread(get_service().for_analysis, lat, lng, radius_m, device_hash)
