"""基于真实路网的等时圈算法。

百度地图不提供等时圈接口，也不开放底层路网数据。本算法用**扇形采样 + 射线插值**
在仅有「批量算路」这一原语的条件下逼近真实可达边界。出行方式（步行 / 骑行 / 驾车）
决定用哪张路网、采样半径上界，以及驾车是否计入实时路况。

步行时再做一步**精细化**（见 refine.py）：批量算路的耗时不含过街等待，
每个方向补一条步行路线，把其中的过街与路口延误补回采样点耗时；
用户标注的施工围挡若被路线穿过，该方向在围挡处截断。
"""

from __future__ import annotations

import asyncio
import statistics
from dataclasses import dataclass, field

from ..baidu.client import BaiduMapClient
from ..baidu.errors import IncompleteSamplingError
from ..travel import WALK, TravelMode, get_mode
from .geometry import offset_point, polygon_area_m2, simplify_path, smooth_radii
from .refine import DEFAULT_BASE_SPEED, RefineOptions, RefineResult, build_profile


@dataclass(frozen=True)
class IsochroneConfig:
    minutes: float = 15.0
    directions: int = 36
    mode_id: str = "walk"
    # None 表示采用该出行方式的默认半径梯度。驾车 15 分钟直线约 9 公里，
    # 若仍用步行的 1400 米上界，整圈都会饱和，形状失去意义。
    radii_m: tuple[float, ...] | None = None
    smooth_window: int = 3

    @property
    def mode(self) -> TravelMode:
        return get_mode(self.mode_id)

    @property
    def sampling_radii(self) -> tuple[float, ...]:
        return self.radii_m or self.mode.radii_m

    @property
    def target_seconds(self) -> float:
        return self.minutes * 60.0


@dataclass
class RaySample:
    """单条射线上一个采样点的测量结果。"""

    radius_m: float
    lat: float
    lng: float
    duration_s: float | None = None
    distance_m: float | None = None
    # 步行路线补回的过街与路口等待（秒）。批量算路本身不含这部分。
    delay_s: float = 0.0
    # 同方向路线在此之前穿过了用户标注的施工围挡
    blocked: bool = False

    @property
    def reachable(self) -> bool:
        return self.duration_s is not None

    @property
    def effective_s(self) -> float | None:
        """用于求边界的耗时：批量算路耗时 + 过街等待；受阻或算路失败为 None。"""
        if self.duration_s is None or self.blocked:
            return None
        return self.duration_s + self.delay_s


@dataclass
class RayResult:
    bearing_deg: float
    boundary_m: float
    boundary_lat: float
    boundary_lng: float
    samples: list[RaySample] = field(default_factory=list)
    truncated_by_barrier: bool = False
    saturated: bool = False  # 采样上界内始终未超时，真实边界可能更远
    # 截断原因是用户标注的施工围挡（而非算路不可达）
    truncated_by_closure: bool = False
    # 边界以内已累计的过街等待（秒）与过街次数
    delay_s: float = 0.0
    crossings: int = 0
    # 该方向的步行路线：ok / failed / skipped（未请求）
    route: str = "skipped"

    @property
    def detour_ratio(self) -> float | None:
        """绕行系数：真实路网距离 ÷ 直线距离。

        反映道路的迂回程度，是"直线距离不等于真实可达"最直观的量化证据。
        取边界附近最外侧的有效采样点计算。
        """
        for s in reversed(self.samples):
            if s.reachable and s.distance_m and s.radius_m > 0:
                # 采样点是沿方位角推进 radius_m 米生成的，radius_m 即直线距离
                return round(s.distance_m / s.radius_m, 3)
        return None


@dataclass
class Isochrone:
    center: tuple[float, float]
    minutes: float
    rays: list[RayResult]
    polygon: list[tuple[float, float]]
    area_m2: float
    mean_radius_m: float
    min_radius_m: float
    max_radius_m: float
    sampled_points: int
    failed_points: int
    mode: TravelMode = WALK
    # 同一批采样再插值出的内圈（5/10 分钟）。不另耗配额。
    nested: list[tuple[float, list[tuple[float, float]]]] = field(default_factory=list)
    # 未做过街校正与围挡截断时的外圈，供前端画虚线对比
    raw_polygon: list[tuple[float, float]] = field(default_factory=list)
    refine: RefineResult | None = None

    @property
    def compactness(self) -> float:
        """紧凑度：最小方向半径 ÷ 最大方向半径，取值 0~1。

        越接近 1 说明各方向可达性越均衡；明显偏低意味着存在铁路、河道、
        高架这类切割，是后续盲区分析的重要线索。
        """
        return round(self.min_radius_m / self.max_radius_m, 3) if self.max_radius_m else 0.0

    def _route_baseline(self, i: int) -> dict:
        """第 i 个方向的步行路线摘要：终点、路网距离、化简后的折线（[经度, 纬度]）。

        这是复测巡检的基线。折线按 4 米容差化简，一条 1 公里多的路线通常只剩二三十个点，
        判断几十米尺度的分岔绰绰有余，结果文件也不会因此膨胀。
        """
        ref = self.refine
        if ref is None or i >= len(ref.routes) or i >= len(ref.targets):
            return {}
        route, target = ref.routes[i], ref.targets[i]
        if route is None or target is None:
            return {}
        pts = [
            (float(p[0]), float(p[1]))
            for step in route.get("steps") or []
            for p in step.get("path") or []
        ]
        out: dict = {
            "route_to": [round(target[0], 6), round(target[1], 6)],
            "route_m": round(float(route.get("distance_m") or 0.0), 1),
            "route_path": [[round(lng, 6), round(lat, 6)] for lat, lng in simplify_path(pts, 4.0)],
        }
        if route.get("fetched_at"):
            out["route_fetched_at"] = route["fetched_at"]
        return out

    def to_geojson(self) -> dict:
        """输出 GeoJSON。注意坐标序为 [经度, 纬度]，与内部 (lat, lng) 相反。"""
        ring = _closed_ring(self.polygon)
        detours = [r.detour_ratio for r in self.rays if r.detour_ratio]
        props: dict = {
            "minutes": self.minutes,
            "area_m2": round(self.area_m2, 1),
            "area_km2": round(self.area_m2 / 1e6, 4),
            "mean_radius_m": round(self.mean_radius_m, 1),
            "min_radius_m": round(self.min_radius_m, 1),
            "max_radius_m": round(self.max_radius_m, 1),
            "compactness": self.compactness,
            "mean_detour": round(sum(detours) / len(detours), 3) if detours else None,
            "max_detour": round(max(detours), 3) if detours else None,
            "sampled_points": self.sampled_points,
            "failed_points": self.failed_points,
            "mode": self.mode.id,
            "mode_label": self.mode.label,
            "uses_traffic": self.mode.uses_traffic,
            "speed_m_per_s": self.mode.speed_m_per_s,
            "factors": list(self.mode.factors),
            # 各方向半径供雷达图使用；不含 POI 原始记录
            "rays": [
                {
                    "bearing": round(r.bearing_deg, 1),
                    "radius_m": round(r.boundary_m, 1),
                    "detour": r.detour_ratio,
                    "barrier": r.truncated_by_barrier,
                    "closure": r.truncated_by_closure,
                    "delay_s": round(r.delay_s, 1),
                    "crossings": r.crossings,
                    "route": r.route,
                    **self._route_baseline(i),
                }
                for i, r in enumerate(self.rays)
            ],
            # 算法质量诊断：等时圈是插值逼近，必须让用户知道哪些方向是
            # 截断/饱和/首点即失败，而不是把多边形当成确定结果。
            "quality": {
                "directions": len(self.rays),
                "saturated": sum(1 for r in self.rays if r.saturated),
                "barrier_truncated": sum(1 for r in self.rays if r.truncated_by_barrier),
                "closure_truncated": sum(1 for r in self.rays if r.truncated_by_closure),
                "zero_radius": sum(1 for r in self.rays if r.boundary_m <= 0),
                "route_failed": sum(1 for r in self.rays if r.route == "failed"),
            },
            # 内圈与外圈来自同一次批量算路，只是阈值不同
            "rings": [
                {"minutes": minutes, "coordinates": _closed_ring(poly)}
                for minutes, poly in self.nested
            ],
        }
        if self.refine is not None:
            props["delay"] = self.refine.as_dict()
            props["raw_ring"] = _closed_ring(self.raw_polygon)
        return {
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": props,
        }


def _solve_boundary(samples: list[RaySample], target_s: float) -> tuple[float, bool, bool]:
    """在一条射线上反解边界半径。

    返回 (边界半径, 是否被障碍截断, 是否采样上界内未超时)。

    从内向外扫描，遇到三种终止情形：
      - 采样点不可达（或同方向路线穿过围挡）-> 障碍，边界取上一个达标点；
      - 耗时超过阈值 -> 在该点与前一点之间线性插值；
      - 扫到最外仍达标 -> 标记饱和，边界取采样上界。
    """
    last_ok_radius = 0.0
    last_ok_duration = 0.0

    for s in samples:
        t = s.effective_s
        if t is None:
            return last_ok_radius, True, False
        if t > target_s:
            span_r = s.radius_m - last_ok_radius
            span_t = t - last_ok_duration
            if span_t <= 0:
                return last_ok_radius, False, False
            ratio = (target_s - last_ok_duration) / span_t
            return last_ok_radius + span_r * max(0.0, min(1.0, ratio)), False, False
        last_ok_radius = s.radius_m
        last_ok_duration = t

    return last_ok_radius, False, True


def _route_target(samples: list[RaySample], target_s: float) -> RaySample | None:
    """该方向需要取路线的终点：第一个超时的采样点；都没超时取最外一个可达点。

    补回等待只会让耗时变长、边界内缩，所以到这个点的路线已覆盖求边界要用的全部采样点。
    """
    last_ok: RaySample | None = None
    for s in samples:
        if s.duration_s is None:
            return last_ok
        if s.duration_s > target_s:
            return s
        last_ok = s
    return last_ok


def _base_speed(rays: list[list[RaySample]]) -> float:
    speeds = [
        s.distance_m / s.duration_s
        for ray in rays
        for s in ray
        if s.distance_m and s.duration_s and s.duration_s > 0
    ]
    return statistics.median(speeds) if speeds else DEFAULT_BASE_SPEED


def _polygon_from(
    center: tuple[float, float],
    bearings: list[float],
    rays: list[list[RaySample]],
    target_s: float,
    smooth_window: int,
) -> list[tuple[float, float]]:
    """按采样点当前的有效耗时求一圈边界。"""
    radii = smooth_radii([_solve_boundary(ray, target_s)[0] for ray in rays], smooth_window)
    lat0, lng0 = center
    return [
        offset_point(lat0, lng0, bearing, radius)
        for bearing, radius in zip(bearings, radii, strict=True)
    ]


async def _refine_rays(
    client: BaiduMapClient,
    center: tuple[float, float],
    rays: list[list[RaySample]],
    bearings: list[float],
    target_s: float,
    options: RefineOptions,
) -> RefineResult:
    """每个方向取一条步行路线，把过街等待与围挡受阻写回采样点。"""
    base = _base_speed(rays)
    targets = [_route_target(ray, target_s) for ray in rays]

    async def fetch(target: RaySample | None):
        if target is None:
            return None
        return await client.walking_route(center, (target.lat, target.lng))

    routes = await asyncio.gather(*(fetch(t) for t in targets))
    profiles = [
        build_profile(route, base, options.closures) if route is not None else None
        for route in routes
    ]

    for ray, profile in zip(rays, profiles, strict=True):
        if profile is None:
            continue
        for s in ray:
            if s.distance_m is None:
                continue
            if options.delay:
                s.delay_s = profile.delay_at(s.distance_m)
            if profile.blocked_at_m is not None and s.distance_m >= profile.blocked_at_m:
                s.blocked = True

    return RefineResult(
        bearings=bearings,
        profiles=profiles,
        base_speed=base,
        delay_applied=options.delay,
        closures=options.closures,
        routes_requested=sum(1 for t in targets if t is not None),
        routes_ok=sum(1 for p in profiles if p is not None),
        routes=list(routes),
        targets=[(t.lat, t.lng) if t is not None else None for t in targets],
    )


async def compute_isochrone(
    client: BaiduMapClient,
    center: tuple[float, float],
    config: IsochroneConfig | None = None,
    refine: RefineOptions | None = None,
) -> Isochrone:
    cfg = config or IsochroneConfig()
    lat0, lng0 = center
    radii = cfg.sampling_radii

    bearings = [360.0 * d / cfg.directions for d in range(cfg.directions)]
    rays: list[list[RaySample]] = []
    flat: list[tuple[float, float]] = []
    for bearing in bearings:
        ray: list[RaySample] = []
        for r in radii:
            plat, plng = offset_point(lat0, lng0, bearing, r)
            ray.append(RaySample(radius_m=r, lat=plat, lng=plng))
            flat.append((plat, plng))
        rays.append(ray)

    failures_before = len(getattr(client, "matrix_failures", []))
    matrix = await client.route_matrix(cfg.mode_id, (lat0, lng0), flat)
    failed_blocks = getattr(client, "matrix_failures", [])[failures_before:]
    if failed_blocks:
        # 整批请求失败（限流重试耗尽、网络中断）时这些点「没测到」，不是「走不到」。
        # 把它们当障碍会画出一个缩水甚至为 0 的圈并照常出分，宁可报错也不出这种圈。
        raise IncompleteSamplingError(failed_blocks, cfg.mode.endpoint)

    failed = 0
    for i, entry in enumerate(matrix):
        sample = rays[i // len(radii)][i % len(radii)]
        if entry is None:
            failed += 1
            continue
        sample.duration_s = entry["duration_s"]
        sample.distance_m = entry["distance_m"]

    # 未校正的外圈先算出来，供对比
    raw_polygon = _polygon_from(center, bearings, rays, cfg.target_seconds, cfg.smooth_window)

    refine_result: RefineResult | None = None
    # 过街校正只对步行有意义：骑行 / 驾车的路线接口与路网完全不同
    if refine is not None and refine.needs_routes and cfg.mode.id == WALK.id:
        refine_result = await _refine_rays(
            client, center, rays, bearings, cfg.target_seconds, refine
        )

    raw = [_solve_boundary(ray, cfg.target_seconds) for ray in rays]
    smoothed = smooth_radii([r[0] for r in raw], cfg.smooth_window)

    results: list[RayResult] = []
    polygon: list[tuple[float, float]] = []
    for d, (bearing, ray, (_, barrier, saturated), radius) in enumerate(
        zip(bearings, rays, raw, smoothed, strict=True)
    ):
        blat, blng = offset_point(lat0, lng0, bearing, radius)
        polygon.append((blat, blng))
        first_bad = next((s for s in ray if s.effective_s is None), None)
        inside = [s for s in ray if s.effective_s is not None and s.radius_m <= radius]
        profile = refine_result.profiles[d] if refine_result else None
        edge = inside[-1] if inside else None
        results.append(
            RayResult(
                bearing_deg=bearing,
                boundary_m=radius,
                boundary_lat=blat,
                boundary_lng=blng,
                samples=ray,
                truncated_by_barrier=barrier,
                saturated=saturated,
                truncated_by_closure=bool(barrier and first_bad is not None and first_bad.blocked),
                delay_s=edge.delay_s if edge else 0.0,
                crossings=(
                    profile.crossings_before(edge.distance_m or 0.0)
                    if profile is not None and edge is not None
                    else 0
                ),
                route=(
                    "skipped"
                    if refine_result is None
                    else ("ok" if profile is not None else "failed")
                ),
            )
        )

    area = polygon_area_m2(polygon)
    if refine_result is not None:
        refine_result.raw_area_m2 = polygon_area_m2(raw_polygon)
        refine_result.area_m2 = area
        refine_result.boundary_crossings = sum(r.crossings for r in results)
        refine_result.closure_rays = sum(1 for r in results if r.truncated_by_closure)

    positive = [r for r in smoothed if r > 0] or [0.0]
    nested = [
        (m, _polygon_at(center, results, m * 60.0, cfg.smooth_window))
        for m in (5.0, 10.0)
        if m < cfg.minutes - 0.1
    ]
    return Isochrone(
        center=center,
        minutes=cfg.minutes,
        rays=results,
        polygon=polygon,
        area_m2=area,
        mean_radius_m=sum(smoothed) / len(smoothed),
        min_radius_m=min(positive),
        max_radius_m=max(smoothed),
        sampled_points=len(flat),
        failed_points=failed,
        mode=cfg.mode,
        nested=nested,
        raw_polygon=raw_polygon,
        refine=refine_result,
    )


def _closed_ring(polygon: list[tuple[float, float]]) -> list[list[float]]:
    ring = [[lng, lat] for lat, lng in polygon]
    if ring and ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring


def _polygon_at(
    center: tuple[float, float],
    rays: list[RayResult],
    target_s: float,
    smooth_window: int,
) -> list[tuple[float, float]]:
    """用已经测好的采样点再插值一圈。采样没变，所以不产生新的算路。"""
    raw = [_solve_boundary(ray.samples, target_s)[0] for ray in rays]
    radii = smooth_radii(raw, smooth_window)
    lat0, lng0 = center
    return [
        offset_point(lat0, lng0, ray.bearing_deg, radius)
        for ray, radius in zip(rays, radii, strict=True)
    ]
