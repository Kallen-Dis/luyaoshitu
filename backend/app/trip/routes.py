"""出行接口与校验；挂载依赖由 main 注入，避免循环导入。"""

from __future__ import annotations

import copy
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from ..baidu.client import request_observer
from ..baidu.errors import BaiduApiError
from ..markings import apply as marking_apply
from ..markings.models import clean_text
from ..markings.routes import get_service, markings_for_analysis
from ..markings.service import MarkingError
from ..markings.trust import RateLimited
from ..poi.catalog import CATEGORIES, by_name
from .budget import BudgetExhausted, TripBudget
from .candidates import TripError, candidates, distance, place_id, point
from .context import overlay
from .feedback import FreshFeedbackStore
from .guide import (
    adopted_ids,
    check_origin,
    fixed_scope,
    nearby_feature,
    relevant_evidence,
    route_scope,
    routing_feature,
)
from .service import TripService
from .supplement import supplement


class NearestIn(BaseModel):
    feature: dict[str, Any]
    origin: dict[str, Any]
    category: str = Field(max_length=32)
    limit: int = 5
    target_place_id: str | None = Field(None, max_length=64)
    include_pending: bool = False
    poi_query: str | None = Field(None, min_length=2, max_length=60)


class PlanIn(BaseModel):
    feature: dict[str, Any]
    origin: dict[str, Any]
    stops: list[str]
    selected_stops: list[dict[str, Any]] | None = None
    replace_stop: dict[str, Any] | None = None
    retry_routes: bool = False
    fixed_stops: list[dict[str, Any]] | None = Field(None, max_length=len(CATEGORIES))


class OptionsIn(BaseModel):
    feature: dict[str, Any]
    origin: dict[str, Any]
    categories: list[str] = Field(min_length=1, max_length=6)
    poi_query: str | None = Field(None, min_length=2, max_length=60)


class FeedbackPlace(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    category: str = Field(pattern="^生鲜采买$")
    lat: float = Field(ge=3.8, le=53.6)
    lng: float = Field(ge=73.4, le=135.1)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        return clean_text(value, 200, "门店名称", required=True)

    def place(self):
        return self.model_dump()


class FeedbackSummaryIn(BaseModel):
    places: list[FeedbackPlace] = Field(min_length=1, max_length=50)


class FeedbackIn(BaseModel):
    place: FeedbackPlace
    vote: int = Field(strict=True, ge=-1, le=1)


class ReanchorIn(BaseModel):
    feature: dict[str, Any]
    origin: dict[str, Any]
    stops: list[str] = Field(min_length=1, max_length=len(CATEGORIES))
    selected_stops: list[dict[str, Any]] = Field(min_length=1, max_length=len(CATEGORIES))
    places: list[dict[str, Any]] = Field(default_factory=list, max_length=len(CATEGORIES))


class NearbyIn(BaseModel):
    feature: dict[str, Any]
    origin: dict[str, Any]
    category: str = Field(max_length=32)


def install(app: FastAPI, prepared, require_ak, handle_error) -> None:
    router = APIRouter(prefix="/api/trip", tags=["trip"])

    @router.post("/fresh-feedback/summary", summary="门店近期售菜反馈（不影响路线）")
    def feedback_summary(req: FeedbackSummaryIn, request: Request):
        svc = get_service()
        viewer = svc.viewer(
            request.headers.get("X-Device-Id"), request.client.host if request.client else None
        )
        try:
            svc.limiter.hit("fresh-summary", f"i:{viewer.ip}", 600, 3600)
        except RateLimited as exc:
            raise MarkingError(
                429, "rate_limited", "反馈读取过于频繁，请稍后再试", retry_after_s=exc.retry_after_s
            ) from exc
        store = FreshFeedbackStore(svc.store.root)
        return {"items": store.summaries([p.place() for p in req.places], viewer.device_hash)}

    @router.put("/fresh-feedback", summary="原地提交、修改或撤回售菜反馈")
    def feedback_vote(req: FeedbackIn, request: Request):
        svc = get_service()
        viewer = svc.viewer(
            request.headers.get("X-Device-Id"), request.client.host if request.client else None
        )
        device = svc._device(viewer)
        svc._limit("vote", viewer)
        return FreshFeedbackStore(svc.store.root).vote(req.place.place(), device, req.vote)

    async def service(
        request: Request, req, guide_mode: str | None = None, *, consume_request: bool = True
    ) -> TripService:
        if req.feature.get("properties", {}).get("planning_preview"):
            raise TripError("planning_preview", "规划模拟结果不能作为正式出行输入。")
        if (
            len((req.feature.get("properties", {}).get("coverage") or {}).get("places") or [])
            > 2000
        ):
            raise TripError("invalid", "设施列表超过单次规划上限。")
        settings = request.app.state.baidu._s
        budget = TripBudget(settings.markings_dir / "trip-budget.sqlite3", settings)
        if consume_request and not budget.take_request():
            raise HTTPException(
                429,
                detail={
                    "code": "trip_rate_limited",
                    "message": "出行规划小时请求次数已达上限，请稍后重试。",
                },
                headers={"Retry-After": "3600"},
            )
        feature = prepared(copy.deepcopy(req.feature))
        origin = req.origin
        usage = None
        if guide_mode:
            if feature["properties"].get("simulated"):
                raise TripError("invalid", "模拟结果不能用于当前位置步行。")
            check_origin(origin)
            origin = {**origin, "kind": "map"}  # 所有定位衍生缓存仅在内存中。
            if guide_mode == "nearby":
                require_ak()
                feature, usage = await nearby_feature(
                    request.app.state.baidu, settings, origin, req.category, feature
                )
            else:
                fixed_scope(feature, origin, req.places)
        if "places" not in (feature["properties"].get("coverage") or {}):
            raise TripError("no_places", "这份结果没有设施列表，请打开设施覆盖重新计算。")
        if not feature["properties"].get("simulated"):
            props = feature["properties"]
            center = point(props["center"])
            radius = float((props.get("coverage") or {}).get("radius_m") or 2500)
            viewer = get_service().viewer(request.headers.get("X-Device-Id"), None)
            nearby = await markings_for_analysis(*center, radius, viewer.device_hash)
            adopted = adopted_ids(feature)
            overlay(feature, marking_apply.plan(nearby, "auto", include=adopted))
            if guide_mode:
                feature["properties"]["markings"]["guide_adopted_ids"] = adopted
        result = TripService(request.app.state.baidu, feature, origin, usage=usage)
        if consume_request and not feature["properties"].get("simulated"):
            require_ak()
        return result

    @router.post("/options", summary="浏览可选设施及入口；按店名补查需显式提交查询")
    async def options(req: OptionsIn, request: Request) -> dict:
        try:
            if len(set(req.categories)) != len(req.categories) or any(
                by_name(c) is None for c in req.categories
            ):
                raise TripError("invalid", "设施类别无效或重复。")
            query = req.poi_query.strip() if req.poi_query is not None else None
            if query is not None and (len(query) < 2 or req.categories != ["生鲜采买"]):
                raise TripError("invalid", "按店名补查需选择生鲜采买，并输入至少两个字。")
            if query and req.feature.get("properties", {}).get("simulated"):
                raise TripError("invalid", "模拟结果不能按店名补查真实设施。")
            svc = await service(request, req, consume_request=bool(query))

            # 普通浏览只同步已补查的本地召回索引，不产生百度请求。
            def observe_lookup(endpoint, params):
                if endpoint in (
                    "/place/v2/suggestion",
                    "/place/v2/detail",
                    "/reverse_geocoding/v3/",
                ):
                    svc.budget.take_poi(svc.usage)
                else:
                    svc.budget.observe(svc.usage, endpoint, params)

            token = request_observer.set(observe_lookup)
            try:
                warnings = await supplement(
                    request.app.state.baidu,
                    svc.feature,
                    req.categories,
                    query,
                    skip_keywords=True,
                )
            finally:
                request_observer.reset(token)
            aliases = {
                place_id(p): [a[:200] for a in p["aliases"] if isinstance(a, str)][:6]
                if isinstance(p.get("aliases"), list)
                else []
                for p in svc.feature["properties"]["coverage"]["places"]
            }
            groups = []
            for category in req.categories:
                try:
                    nodes = candidates(svc.feature, category, svc.closures)
                except TripError as exc:
                    if exc.code != "category_failed":
                        raise
                    groups.append({"category": category, "items": [], "error": str(exc)})
                else:
                    groups.append(
                        {
                            "category": category,
                            "items": [
                                {
                                    **node.as_dict(),
                                    "straight_m": round(distance(svc.origin, node)),
                                    "aliases": aliases.get(node.place_id, []),
                                }
                                for node in sorted(
                                    nodes, key=lambda n: (distance(svc.origin, n), n.id)
                                )
                            ],
                        }
                    )
            result = {"groups": groups, "preview": svc.simulated, "warnings": warnings}
            if query:
                result["quota"] = {
                    **asdict(svc.usage),
                    "remaining": svc.budget.remaining(svc.usage),
                }
            return result
        except TripError as exc:
            raise HTTPException(400, detail={"code": exc.code, "message": str(exc)}) from exc
        except BudgetExhausted as exc:
            raise HTTPException(429, detail={"code": "trip_budget", "message": str(exc)}) from exc
        except BaiduApiError as exc:
            raise handle_error(exc, "trip") from exc

    @router.post("/reanchor", summary="保持所选设施、入口和顺序，从新起点重规划")
    async def reanchor(req: ReanchorIn, request: Request):
        try:
            if (
                len(req.stops) != len(req.selected_stops)
                or len(set(req.stops)) != len(req.stops)
                or any(by_name(c) is None for c in req.stops)
            ):
                raise TripError("invalid", "所选站点与类别不一致。")
            svc = await service(request, req, "fixed")
            result = await svc.reanchor(req.stops, req.selected_stops)
            if result["routing_status"] == "clear":
                # 路线可能绕出起终点的初始查询范围；按实际折线再次读取共享证据。
                center, radius, paths = route_scope(result)
                viewer = get_service().viewer(request.headers.get("X-Device-Id"), None)
                latest = await markings_for_analysis(*center, radius, viewer.device_hash)
                adopted = adopted_ids(req.feature)
                evidence = marking_apply.plan(
                    relevant_evidence(latest, result, paths), "auto", include=adopted
                )
                overlay(svc.feature, evidence)
                if any(
                    m["type"] == "closure" and "上限" in m.get("reason", "")
                    for m in evidence.skipped
                ):
                    raise TripError("too_many_closures", "沿途围挡超过核验上限，原计划保留。")
                svc = TripService(
                    request.app.state.baidu,
                    svc.feature,
                    {**req.origin, "kind": "map"},
                    usage=svc.usage,
                )
                result = await svc.reanchor(req.stops, req.selected_stops)
            return {**result, "routing_feature": routing_feature(svc.feature)}
        except TripError as exc:
            raise HTTPException(400, detail={"code": exc.code, "message": str(exc)}) from exc
        except BudgetExhausted as exc:
            raise HTTPException(429, detail={"code": "trip_budget", "message": str(exc)}) from exc
        except BaiduApiError as exc:
            raise handle_error(exc, "trip") from exc

    @router.post("/guide-nearby", summary="从当前位置补检索同类型设施并按步行距离排序")
    async def guide_nearby(req: NearbyIn, request: Request):
        try:
            if by_name(req.category) is None:
                raise TripError("invalid", "设施类别无效。")
            svc = await service(request, req, "nearby")
            result = await svc.nearest(req.category, 5, refresh_pois=False)
            return {**result, "routing_feature": routing_feature(svc.feature)}
        except TripError as exc:
            raise HTTPException(400, detail={"code": exc.code, "message": str(exc)}) from exc
        except BudgetExhausted as exc:
            raise HTTPException(429, detail={"code": "trip_budget", "message": str(exc)}) from exc
        except BaiduApiError as exc:
            raise handle_error(exc, "trip") from exc

    @router.post("/nearest", summary="已检索设施中的最近几家")
    async def nearest(req: NearestIn, request: Request) -> dict:
        try:
            if by_name(req.category) is None or not 1 <= req.limit <= 5:
                raise TripError("invalid", "类别无效，或最近设施数量不在 1～5 之间。")
            query = req.poi_query.strip() if req.poi_query else None
            if query is not None and (len(query) < 2 or req.category != "生鲜采买"):
                raise TripError("invalid", "按店名补查需选择生鲜采买，并输入至少两个字。")
            return await (await service(request, req)).nearest(
                req.category, req.limit, req.target_place_id, req.include_pending, query
            )
        except TripError as exc:
            raise HTTPException(400, detail={"code": exc.code, "message": str(exc)}) from exc
        except BaiduApiError as exc:
            raise handle_error(exc, "trip") from exc

    @router.post("/plan", summary="按用户站序规划步行行程")
    async def plan(req: PlanIn, request: Request) -> dict:
        try:
            if (
                not 1 <= len(req.stops) <= len(CATEGORIES)
                or len(set(req.stops)) != len(req.stops)
                or any(by_name(c) is None for c in req.stops)
            ):
                raise TripError("invalid", "请选择 1～6 个不同的有效品类，按希望到访的顺序排列。")
            if req.replace_stop is not None and req.selected_stops is None:
                raise TripError("invalid", "换一家必须提供当前已选站点。")
            if req.retry_routes and (req.selected_stops is None or req.replace_stop is not None):
                raise TripError("invalid", "补取路线必须固定当前站点，不能同时换站。")
            if req.fixed_stops and (
                req.selected_stops is not None or req.replace_stop is not None or req.retry_routes
            ):
                raise TripError("invalid", "设置行程的指定设施不能与更换或补取路线同时提交。")
            return await (await service(request, req)).plan(
                req.stops, req.selected_stops, req.replace_stop, req.retry_routes, req.fixed_stops
            )
        except TripError as exc:
            raise HTTPException(400, detail={"code": exc.code, "message": str(exc)}) from exc
        except BaiduApiError as exc:
            raise handle_error(exc, "trip") from exc

    app.include_router(router)
