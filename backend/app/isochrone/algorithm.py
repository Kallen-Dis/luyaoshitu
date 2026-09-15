"""基于真实路网的步行等时圈算法。

百度地图不提供等时圈接口，也不开放底层路网数据。本算法用**扇形采样 + 射线插值**
在仅有「批量算路」这一原语的条件下逼近真实可达边界：

1. 以中心点为原点，按方位角均匀发射 N 条射线；
2. 每条射线上按固定半径梯度布置候选点；
3. 所有候选点一次性提交批量算路，取回真实路网步行耗时；
4. 在每条射线上找到耗时首次超过阈值的区间，线性插值反解出边界半径；
5. 环形平滑各方向半径，连成多边形。

关键设计取舍：

- **首次穿越而非最远可达**。某方向 600 米处耗时超标、900 米处却达标（绕行到了
  另一条快速通道），边界仍取 600 米。等时圈描述的是连通可达区域，
  跨越不可达地带的"飞地"不应计入。
- **失败点视为障碍而非跳过**。算路返回不可达时，射线在此截断。把它当缺失值忽略
  会让等时圈越过河道、铁路这类真实屏障，得出偏乐观的结论。
- **采样点数不是瓶颈**。批量算路单次可提交 100 个终点（实测硬上限），
  36 方向 × 7 档半径 = 252 个点仅需 3 次请求，可以放心提高角分辨率。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..baidu.client import BaiduMapClient
from .geometry import offset_point, polygon_area_m2, smooth_radii


@dataclass(frozen=True)
class IsochroneConfig:
    minutes: float = 15.0
    directions: int = 36
    # 半径梯度。上界 1500 米：15 分钟按常人步行速度（约 1.2 m/s）的直线极限约
    # 1080 米，即便在路网极通畅处也难超 1500 米，再往外采样只是浪费配额。
    radii_m: tuple[float, ...] = (200, 400, 600, 800, 1000, 1200, 1400)
    smooth_window: int = 3

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

    @property
    def reachable(self) -> bool:
        return self.duration_s is not None


@dataclass
class RayResult:
    bearing_deg: float
    boundary_m: float
    boundary_lat: float
    boundary_lng: float
    samples: list[RaySample] = field(default_factory=list)
    truncated_by_barrier: bool = False
    saturated: bool = False  # 采样上界内始终未超时，真实边界可能更远

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

    @property
    def compactness(self) -> float:
        """紧凑度：最小方向半径 ÷ 最大方向半径，取值 0~1。

        越接近 1 说明各方向可达性越均衡；明显偏低意味着存在铁路、河道、
        高架这类切割，是后续盲区分析的重要线索。
        """
        return round(self.min_radius_m / self.max_radius_m, 3) if self.max_radius_m else 0.0

    def to_geojson(self) -> dict:
        """输出 GeoJSON。注意坐标序为 [经度, 纬度]，与内部 (lat, lng) 相反。"""
        ring = [[lng, lat] for lat, lng in self.polygon]
        if ring and ring[0] != ring[-1]:
            ring.append(ring[0])
        detours = [r.detour_ratio for r in self.rays if r.detour_ratio]
        return {
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": {
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
                # 各方向半径供雷达图使用；不含 POI 原始记录
                "rays": [
                    {
                        "bearing": round(r.bearing_deg, 1),
                        "radius_m": round(r.boundary_m, 1),
                        "detour": r.detour_ratio,
                        "barrier": r.truncated_by_barrier,
                    }
                    for r in self.rays
                ],
            },
        }


def _solve_boundary(
    samples: list[RaySample], target_s: float
) -> tuple[float, bool, bool]:
    """在一条射线上反解边界半径。

    返回 (边界半径, 是否被障碍截断, 是否采样上界内未超时)。

    从内向外扫描，遇到三种终止情形：
      - 采样点不可达 -> 障碍，边界取上一个达标点；
      - 耗时超过阈值 -> 在该点与前一点之间线性插值；
      - 扫到最外仍达标 -> 标记饱和，边界取采样上界。
    """
    last_ok_radius = 0.0
    last_ok_duration = 0.0

    for s in samples:
        if not s.reachable:
            return last_ok_radius, True, False
        assert s.duration_s is not None
        if s.duration_s > target_s:
            span_r = s.radius_m - last_ok_radius
            span_t = s.duration_s - last_ok_duration
            if span_t <= 0:
                return last_ok_radius, False, False
            ratio = (target_s - last_ok_duration) / span_t
            return last_ok_radius + span_r * max(0.0, min(1.0, ratio)), False, False
        last_ok_radius = s.radius_m
        last_ok_duration = s.duration_s

    return last_ok_radius, False, True


async def compute_isochrone(
    client: BaiduMapClient,
    center: tuple[float, float],
    config: IsochroneConfig | None = None,
) -> Isochrone:
    cfg = config or IsochroneConfig()
    lat0, lng0 = center

    # 构造全部采样点：方位角均匀，半径按梯度
    rays: list[list[RaySample]] = []
    flat: list[tuple[float, float]] = []
    for d in range(cfg.directions):
        bearing = 360.0 * d / cfg.directions
        ray: list[RaySample] = []
        for r in cfg.radii_m:
            plat, plng = offset_point(lat0, lng0, bearing, r)
            ray.append(RaySample(radius_m=r, lat=plat, lng=plng))
            flat.append((plat, plng))
        rays.append(ray)

    matrix = await client.walking_matrix((lat0, lng0), flat)

    failed = 0
    for i, entry in enumerate(matrix):
        sample = rays[i // len(cfg.radii_m)][i % len(cfg.radii_m)]
        if entry is None:
            failed += 1
            continue
        sample.duration_s = entry["duration_s"]
        sample.distance_m = entry["distance_m"]

    # 逐射线求解边界
    raw: list[tuple[float, bool, bool]] = [
        _solve_boundary(ray, cfg.target_seconds) for ray in rays
    ]
    radii = smooth_radii([r[0] for r in raw], cfg.smooth_window)

    results: list[RayResult] = []
    polygon: list[tuple[float, float]] = []
    for d, (ray, (_, barrier, saturated), radius) in enumerate(
        zip(rays, raw, radii, strict=True)
    ):
        bearing = 360.0 * d / cfg.directions
        blat, blng = offset_point(lat0, lng0, bearing, radius)
        polygon.append((blat, blng))
        results.append(
            RayResult(
                bearing_deg=bearing,
                boundary_m=radius,
                boundary_lat=blat,
                boundary_lng=blng,
                samples=ray,
                truncated_by_barrier=barrier,
                saturated=saturated,
            )
        )

    positive = [r for r in radii if r > 0] or [0.0]
    return Isochrone(
        center=center,
        minutes=cfg.minutes,
        rays=results,
        polygon=polygon,
        area_m2=polygon_area_m2(polygon),
        mean_radius_m=sum(radii) / len(radii),
        min_radius_m=min(positive),
        max_radius_m=max(radii),
        sampled_points=len(flat),
        failed_points=failed,
    )
