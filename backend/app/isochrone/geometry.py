"""等时圈计算所需的球面几何工具。

百度返回的是 BD09 经纬度。在 1~2 公里的尺度上，把局部近似为平面带来的误差
远小于路网本身的不确定性，因此这里用等距圆柱近似而非严格大圆公式——
代价是可忽略的精度损失，换来的是代码简单和计算量小。
"""

from __future__ import annotations

import math

METERS_PER_DEG_LAT = 111_320.0


def meters_per_deg_lng(lat: float) -> float:
    """经度方向上 1 度对应的米数，随纬度收缩。

    上海（31.2°N）约 95.2 km/度，北京（39.9°N）约 85.4 km/度，相差 10% 以上。
    写死常量会让不同城市的采样半径失准，必须按纬度实算。
    """
    return METERS_PER_DEG_LAT * math.cos(math.radians(lat))


def offset_point(
    lat: float, lng: float, bearing_deg: float, distance_m: float
) -> tuple[float, float]:
    """从 (lat,lng) 沿指定方位角推进 distance_m 米。方位角以正北为 0、顺时针为正。"""
    rad = math.radians(bearing_deg)
    north_m = distance_m * math.cos(rad)
    east_m = distance_m * math.sin(rad)
    return (
        lat + north_m / METERS_PER_DEG_LAT,
        lng + east_m / meters_per_deg_lng(lat),
    )


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """两点间直线距离（米）。用于计算绕行系数：真实路网距离 ÷ 直线距离。"""
    dlat = (lat2 - lat1) * METERS_PER_DEG_LAT
    dlng = (lng2 - lng1) * meters_per_deg_lng((lat1 + lat2) / 2)
    return math.hypot(dlat, dlng)


def polygon_area_m2(points: list[tuple[float, float]]) -> float:
    """多边形面积（平方米），用鞋带公式。points 为 (lat, lng) 序列。

    面积是等时圈最直观的量化指标：同样 15 分钟，路网通畅的社区圈得大，
    被铁路、河道、高架切割的社区圈得小。
    """
    if len(points) < 3:
        return 0.0
    lat0 = sum(p[0] for p in points) / len(points)
    mpl = meters_per_deg_lng(lat0)
    xy = [((p[1] - points[0][1]) * mpl, (p[0] - points[0][0]) * METERS_PER_DEG_LAT) for p in points]
    total = 0.0
    for i in range(len(xy)):
        x1, y1 = xy[i]
        x2, y2 = xy[(i + 1) % len(xy)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def point_in_polygon(lat: float, lng: float, polygon: list[tuple[float, float]]) -> bool:
    """射线交叉法判断点是否落在多边形内。polygon 为 (lat, lng) 序列。

    直接在经纬度上做判断：等时圈是由同一中心按方位角生成的星形多边形，
    经度未按纬度缩放只会让整个图形横向等比拉伸，不改变点的内外关系。
    """
    inside = False
    n = len(polygon)
    if n < 3:
        return False
    for i in range(n):
        y1, x1 = polygon[i]
        y2, x2 = polygon[(i + 1) % n]
        # 边跨越水平扫描线，且交点在待测点右侧时翻转内外状态
        if (y1 > lat) != (y2 > lat):
            x_cross = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lng < x_cross:
                inside = not inside
    return inside


def grid_points(
    polygon: list[tuple[float, float]], spacing_m: float
) -> list[tuple[float, float]]:
    """在多边形外接矩形内按固定间距布点，只保留落在多边形内的点。

    网格是盲区识别与热力图的共同载体：间距越小分辨率越高，但网格数按平方增长，
    而每个网格都要对各品类设施测距，配额消耗也随之平方增长。
    """
    if len(polygon) < 3 or spacing_m <= 0:
        return []

    lats = [p[0] for p in polygon]
    lngs = [p[1] for p in polygon]
    lat0 = sum(lats) / len(lats)
    d_lat = spacing_m / METERS_PER_DEG_LAT
    d_lng = spacing_m / meters_per_deg_lng(lat0)

    out: list[tuple[float, float]] = []
    lat = min(lats)
    while lat <= max(lats):
        lng = min(lngs)
        while lng <= max(lngs):
            if point_in_polygon(lat, lng, polygon):
                out.append((lat, lng))
            lng += d_lng
        lat += d_lat
    return out


def smooth_radii(radii: list[float], window: int = 3) -> list[float]:
    """对各方向的边界半径做环形滑动平均。

    逐条射线独立求解会让相邻方向出现锯齿——某个方向恰好撞上围墙或断头路，
    半径就会突然塌陷。适度平滑能让轮廓更贴近真实的连通区域形态。
    窗口不宜过大，否则会抹平铁路、河道这类真实的可达性断裂。
    """
    n = len(radii)
    if n == 0 or window <= 1:
        return list(radii)
    half = window // 2
    return [
        sum(radii[(i + k) % n] for k in range(-half, half + 1)) / (2 * half + 1)
        for i in range(n)
    ]
