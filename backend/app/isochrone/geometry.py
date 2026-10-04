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


def grid_points(polygon: list[tuple[float, float]], spacing_m: float) -> list[tuple[float, float]]:
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


def bearing_deg(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """从点 1 看点 2 的方位角（正北 0°、顺时针），局部平面近似。"""
    north = (lat2 - lat1) * METERS_PER_DEG_LAT
    east = (lng2 - lng1) * meters_per_deg_lng((lat1 + lat2) / 2)
    return math.degrees(math.atan2(east, north)) % 360.0


def decode_baidu_path(path: str) -> list[tuple[float, float]]:
    """把百度路线步骤里的 "lng,lat;lng,lat" 折线转成 (lat, lng) 序列。坏点直接跳过。"""
    out: list[tuple[float, float]] = []
    for pair in (path or "").split(";"):
        parts = pair.split(",")
        if len(parts) != 2:
            continue
        try:
            lng, lat = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        out.append((lat, lng))
    return out


def _segment_hits_circle(
    a: tuple[float, float],
    b: tuple[float, float],
    center: tuple[float, float],
    radius_m: float,
) -> float | None:
    """线段 a→b 第一次进入圆的位置，返回从 a 起算的米数；不相交返回 None。

    在圆心处做局部平面投影后解线段与圆的交点。几百米尺度上平面近似误差远小于围挡半径。
    """
    mpl = meters_per_deg_lng(center[0])

    def xy(p: tuple[float, float]) -> tuple[float, float]:
        return ((p[1] - center[1]) * mpl, (p[0] - center[0]) * METERS_PER_DEG_LAT)

    ax, ay = xy(a)
    bx, by = xy(b)
    dx, dy = bx - ax, by - ay
    seg_len = math.hypot(dx, dy)
    if ax * ax + ay * ay <= radius_m * radius_m:
        return 0.0
    if seg_len == 0:
        return None
    # |a + t·d|² = r²，取 [0,1] 内最小的根
    qa = dx * dx + dy * dy
    qb = 2 * (ax * dx + ay * dy)
    qc = ax * ax + ay * ay - radius_m * radius_m
    disc = qb * qb - 4 * qa * qc
    if disc < 0:
        return None
    t = (-qb - math.sqrt(disc)) / (2 * qa)
    if 0.0 <= t <= 1.0:
        return t * seg_len
    return None


def polyline_length_m(points: list[tuple[float, float]]) -> float:
    return sum(haversine_m(*points[i], *points[i + 1]) for i in range(len(points) - 1))


def first_entry_along(
    points: list[tuple[float, float]],
    circles: list[tuple[float, float, float]],
) -> float | None:
    """折线第一次进入任一圆（lat, lng, 半径米）时已走过的几何长度（米）。从未进入返回 None。"""
    if len(points) < 2 or not circles:
        return None
    walked = 0.0
    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        hits = [
            h
            for lat, lng, r in circles
            if (h := _segment_hits_circle(a, b, (lat, lng), r)) is not None
        ]
        if hits:
            return walked + min(hits)
        walked += haversine_m(*a, *b)
    return None


def _planar(origin: tuple[float, float]):
    """以 origin 为原点的局部平面投影 (lat, lng) → (东, 北) 米。公里尺度上误差可忽略。"""
    mpl = meters_per_deg_lng(origin[0])

    def xy(p: tuple[float, float]) -> tuple[float, float]:
        return ((p[1] - origin[1]) * mpl, (p[0] - origin[0]) * METERS_PER_DEG_LAT)

    return xy


def distance_to_polyline_m(point: tuple[float, float], line: list[tuple[float, float]]) -> float:
    """点到折线的最短距离（米）。折线为空返回无穷大。"""
    if not line:
        return math.inf
    xy = _planar(point)
    if len(line) == 1:
        x, y = xy(line[0])
        return math.hypot(x, y)
    best = math.inf
    prev = xy(line[0])
    for p in line[1:]:
        cur = xy(p)
        dx, dy = cur[0] - prev[0], cur[1] - prev[1]
        seg2 = dx * dx + dy * dy
        # 原点就是 point，求原点到线段的最近点
        t = 0.0 if seg2 == 0 else max(0.0, min(1.0, -(prev[0] * dx + prev[1] * dy) / seg2))
        best = min(best, math.hypot(prev[0] + t * dx, prev[1] + t * dy))
        prev = cur
    return best


def point_along(points: list[tuple[float, float]], s_m: float) -> tuple[float, float]:
    """沿折线走 s_m 米处的点；超出两端时夹到端点。"""
    if not points:
        raise ValueError("空折线")
    if s_m <= 0 or len(points) == 1:
        return points[0]
    walked = 0.0
    for a, b in zip(points, points[1:], strict=False):
        seg = haversine_m(*a, *b)
        if walked + seg >= s_m and seg > 0:
            t = (s_m - walked) / seg
            return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
        walked += seg
    return points[-1]


def resample_polyline(
    points: list[tuple[float, float]], step_m: float
) -> list[tuple[float, tuple[float, float]]]:
    """每隔 step_m 米取一点，返回 [(路径距离, (lat, lng))]，含起点与终点。"""
    if not points:
        return []
    total = polyline_length_m(points)
    out: list[tuple[float, tuple[float, float]]] = []
    s = 0.0
    while s < total:
        out.append((s, point_along(points, s)))
        s += step_m
    out.append((total, points[-1]))
    return out


def simplify_path(
    points: list[tuple[float, float]], tolerance_m: float = 5.0
) -> list[tuple[float, float]]:
    """Douglas–Peucker 折线化简。存路线基线用：几十米尺度的分岔判断不需要逐米的点。"""
    if len(points) <= 2:
        return list(points)
    xy = _planar(points[0])
    pts = [xy(p) for p in points]
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        i, j = stack.pop()
        (ax, ay), (bx, by) = pts[i], pts[j]
        dx, dy = bx - ax, by - ay
        seg = math.hypot(dx, dy)
        worst, idx = 0.0, -1
        for k in range(i + 1, j):
            px, py = pts[k]
            if seg == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                d = abs(dy * (px - ax) - dx * (py - ay)) / seg
            if d > worst:
                worst, idx = d, k
        if idx >= 0 and worst > tolerance_m:
            keep[idx] = True
            stack.append((i, idx))
            stack.append((idx, j))
    return [p for p, k in zip(points, keep, strict=True) if k]


def disc_cells(
    center: tuple[float, float], extent_m: float, spacing_m: float
) -> list[tuple[float, float]]:
    """以中心为原点、按固定间距铺出圆形范围内的格点，(lat, lng) 序列。

    中心 1.5 公里、200 米一格时共 177 个点。与多边形内布点不同，这里覆盖的是
    整个邻里范围：等时圈外、但 1 公里内有设施的居民同样要判定。
    """
    lat0, lng0 = center
    n = math.floor(extent_m / spacing_m)
    mpl = meters_per_deg_lng(lat0)
    out: list[tuple[float, float]] = []
    for iy in range(-n, n + 1):
        for ix in range(-n, n + 1):
            if math.hypot(ix * spacing_m, iy * spacing_m) > extent_m:
                continue
            out.append((lat0 + iy * spacing_m / METERS_PER_DEG_LAT, lng0 + ix * spacing_m / mpl))
    return out


def polygon_of(feature: dict | None) -> list[tuple[float, float]] | None:
    """GeoJSON Feature 的外环转成 (lat, lng) 序列。"""
    if not feature:
        return None
    coords = (feature.get("geometry") or {}).get("coordinates") or []
    if not coords or not coords[0]:
        return None
    return [(float(p[1]), float(p[0])) for p in coords[0] if len(p) >= 2]


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
        sum(radii[(i + k) % n] for k in range(-half, half + 1)) / (2 * half + 1) for i in range(n)
    ]
