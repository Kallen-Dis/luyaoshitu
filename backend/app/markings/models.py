"""标注的类型、字段与校验。

所有写入都先经过 normalize()：把前端提交的字段收成一份规范的 spec（存库、存版本、
比较是否改动都用它），并推导出查询用的代表点与外接框。校验失败抛
MarkingValidationError，消息直接给用户看，所以一律写成中文、说清怎么改。

几何一律 BD09 经纬度；多边形在接口里是 [经度, 纬度] 序列（GeoJSON 惯例）。
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from ..isochrone.geometry import (
    METERS_PER_DEG_LAT,
    distance_to_polyline_m,
    haversine_m,
    meters_per_deg_lng,
    point_in_polygon,
    polygon_area_m2,
)
from ..poi.catalog import CATEGORIES

BEIJING = timezone(timedelta(hours=8))

TYPES: tuple[str, ...] = ("closure", "facility_missing", "facility_extra", "gray_area")

TYPE_LABELS: dict[str, str] = {
    "closure": "围挡 / 封路",
    "facility_missing": "设施失效",
    "facility_extra": "补录设施",
    "gray_area": "灰色区域",
}

# 围挡的种类决定默认有效期（天）：施工与临时封路会结束，封闭小区与断头路基本长期存在
CLOSURE_KINDS: dict[str, int] = {
    "construction": 60,
    "road_closed": 30,
    "gated": 365,
    "barrier": 365,
}
CLOSURE_KIND_LABELS: dict[str, str] = {
    "construction": "施工围挡",
    "road_closed": "临时封路",
    "gated": "封闭小区 / 门禁",
    "barrier": "走不通的路",
}

MISSING_REASONS: dict[str, str] = {
    "closed": "已关闭",
    "not_public": "不对外开放",
    "wrong_location": "位置不对",
    "wrong_category": "类别不对",
}

GRAY_REASONS: dict[str, str] = {
    "capacity": "设施在，但不够用",
    "outside_grid": "在分析范围外",
    "quality": "服务质量差",
    "other": "其他原因",
}

TYPE_TTL_DAYS: dict[str, int] = {
    "facility_missing": 180,
    "facility_extra": 180,
    "gray_area": 365,
}

SOURCES: tuple[str, ...] = ("user", "recheck", "poi", "agent_plan")
SOURCE_LABELS: dict[str, str] = {
    "user": "用户标注",
    "recheck": "复测巡检",
    "poi": "工地检索",
    "agent_plan": "AI 二次核对",
}

CATEGORY_NAMES: tuple[str, ...] = tuple(c.name for c in CATEGORIES)

MIN_TTL_DAYS = 7
MAX_TTL_DAYS = 365
NOTE_MAX = 60
NAME_MAX = 30
CLOSURE_RADIUS_M = (10.0, 300.0)
POLYGON_MAX_VERTICES = 50
POLYGON_MIN_AREA_M2 = 400.0
POLYGON_MAX_AREA_M2 = 1_000_000.0
POLYGON_MAX_SPAN_M = 3000.0

# 中国陆地与近海的粗略外接框。超出范围的坐标多半是经纬度填反了
CHINA_LAT = (3.8, 53.6)
CHINA_LNG = (73.4, 135.1)

# 各类型允许修改的字段；类型本身不能改（改类型等于另一条标注）
EDITABLE: dict[str, tuple[str, ...]] = {
    "closure": ("kind", "lat", "lng", "radius_m", "note"),
    "facility_missing": ("category", "name", "lat", "lng", "reason", "note"),
    "facility_extra": ("category", "name", "lat", "lng", "note"),
    "gray_area": ("polygon", "reason", "categories", "note"),
}


class MarkingValidationError(ValueError):
    """提交的标注不合法。message 直接展示给用户。"""

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


# ---------- 文本 ----------

_CONTROL = re.compile(r"[\x00-\x1f\x7f\u200b-\u200f\u2028-\u202e\u2066-\u2069\ufeff]")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
# 手机号（可带 +86）与带区号的座机。标注是公开的，联系方式不该出现在里面
_PHONE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)|(?<!\d)0\d{2,3}-?\d{7,8}(?!\d)")


def clean_text(value: Any, max_len: int, label: str, *, required: bool = False) -> str:
    """去控制字符与零宽字符、合并空白，并把网址与电话号码替换掉。"""
    text = _CONTROL.sub("", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    text = _URL.sub("［链接已移除］", text)
    text = _PHONE.sub("［号码已移除］", text)
    if required and not text:
        raise MarkingValidationError(f"请填写{label}", label)
    if len(text) > max_len:
        raise MarkingValidationError(f"{label}最多 {max_len} 个字，现在是 {len(text)} 个", label)
    return text


# ---------- 数值与几何 ----------


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise MarkingValidationError(f"{label}不是有效数字", label)
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise MarkingValidationError(f"{label}不是有效数字", label) from exc
    if not math.isfinite(number):
        raise MarkingValidationError(f"{label}不是有效数字", label)
    return number


def check_point(lat: Any, lng: Any, label: str = "位置") -> tuple[float, float]:
    la = _number(lat, f"{label}纬度")
    ln = _number(lng, f"{label}经度")
    if not (CHINA_LAT[0] <= la <= CHINA_LAT[1] and CHINA_LNG[0] <= ln <= CHINA_LNG[1]):
        raise MarkingValidationError(f"{label}不在中国境内，请在地图上重新选点", label)
    return round(la, 6), round(ln, 6)


def _planar(origin: tuple[float, float]):
    mpl = meters_per_deg_lng(origin[0])

    def xy(p: tuple[float, float]) -> tuple[float, float]:
        return ((p[1] - origin[1]) * mpl, (p[0] - origin[0]) * METERS_PER_DEG_LAT)

    return xy


def _cross(o: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _on_segment(p: tuple[float, float], q: tuple[float, float], r: tuple[float, float]) -> bool:
    return (
        min(p[0], r[0]) - 1e-9 <= q[0] <= max(p[0], r[0]) + 1e-9
        and min(p[1], r[1]) - 1e-9 <= q[1] <= max(p[1], r[1]) + 1e-9
    )


def segments_intersect(
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    p4: tuple[float, float],
) -> bool:
    """两条线段是否相交（含端点接触与共线重叠）。平面坐标，单位米。"""
    d1 = _cross(p3, p4, p1)
    d2 = _cross(p3, p4, p2)
    d3 = _cross(p1, p2, p3)
    d4 = _cross(p1, p2, p4)
    eps = 1e-6
    if ((d1 > eps and d2 < -eps) or (d1 < -eps and d2 > eps)) and (
        (d3 > eps and d4 < -eps) or (d3 < -eps and d4 > eps)
    ):
        return True
    return (
        (abs(d1) <= eps and _on_segment(p3, p1, p4))
        or (abs(d2) <= eps and _on_segment(p3, p2, p4))
        or (abs(d3) <= eps and _on_segment(p1, p3, p2))
        or (abs(d4) <= eps and _on_segment(p1, p4, p2))
    )


def check_polygon(raw: Any) -> list[tuple[float, float]]:
    """校验人工灰色区域的多边形，返回 (纬度, 经度) 顶点序列（不含闭合点）。"""
    if not isinstance(raw, list | tuple):
        raise MarkingValidationError("请在地图上画出区域范围", "polygon")
    points: list[tuple[float, float]] = []
    for item in raw:
        if not isinstance(item, list | tuple) or len(item) != 2:
            raise MarkingValidationError("区域顶点格式不对，应为 [经度, 纬度]", "polygon")
        lat, lng = check_point(item[1], item[0], "区域顶点")
        # 连续两次点在几乎同一处（双击）只算一个顶点
        if points and haversine_m(*points[-1], lat, lng) < 1.0:
            continue
        points.append((lat, lng))
    if len(points) >= 2 and haversine_m(*points[0], *points[-1]) < 1.0:
        points.pop()  # 去掉闭合点
    if len(points) < 3:
        raise MarkingValidationError("区域至少需要 3 个顶点", "polygon")
    if len(points) > POLYGON_MAX_VERTICES:
        raise MarkingValidationError(
            f"区域最多 {POLYGON_MAX_VERTICES} 个顶点，请简化轮廓", "polygon"
        )

    xy = _planar(points[0])
    pts = [xy(p) for p in points]
    n = len(pts)
    for i in range(n):
        a, b, c = pts[i - 1], pts[i], pts[(i + 1) % n]
        # 相邻两条边原路折返（尖刺）：面积为零却会让轮廓看起来多出一条线
        if (
            abs(_cross(b, a, c)) < 1e-6
            and (a[0] - b[0]) * (c[0] - b[0]) + (a[1] - b[1]) * (c[1] - b[1]) > 0
        ):
            raise MarkingValidationError("区域轮廓有折返的尖角，请重画", "polygon")
    for i in range(n):
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue  # 相邻边共享顶点，不算相交
            if segments_intersect(pts[i], pts[(i + 1) % n], pts[j], pts[(j + 1) % n]):
                raise MarkingValidationError("区域轮廓自相交了，请按顺序点出边界", "polygon")

    area = polygon_area_m2(points)
    if area < POLYGON_MIN_AREA_M2:
        raise MarkingValidationError("区域太小（不到 400 平方米），请画大一些", "polygon")
    if area > POLYGON_MAX_AREA_M2:
        raise MarkingValidationError(
            f"区域约 {area / 1e6:.2f} 平方公里，超过 1 平方公里上限，请拆成几块分别标注",
            "polygon",
        )
    lats = [p[0] for p in points]
    lngs = [p[1] for p in points]
    span = haversine_m(min(lats), min(lngs), max(lats), max(lngs))
    if span > POLYGON_MAX_SPAN_M:
        raise MarkingValidationError("区域跨度超过 3 公里，请拆成几块分别标注", "polygon")
    return points


def polygon_anchor(points: list[tuple[float, float]]) -> tuple[float, float]:
    """区域的代表点：面积质心落在区域内就用它，否则取离质心最近的顶点边中点。"""
    xy = _planar(points[0])
    pts = [xy(p) for p in points]
    area = 0.0
    cx = cy = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        f = x1 * y2 - x2 * y1
        area += f
        cx += (x1 + x2) * f
        cy += (y1 + y2) * f
    if abs(area) > 1e-9:
        cx /= 3 * area
        cy /= 3 * area
        lat = points[0][0] + cy / METERS_PER_DEG_LAT
        lng = points[0][1] + cx / meters_per_deg_lng(points[0][0])
        if point_in_polygon(lat, lng, points):
            return round(lat, 6), round(lng, 6)
    mids = [
        (
            (points[i][0] + points[(i + 1) % len(points)][0]) / 2,
            (points[i][1] + points[(i + 1) % len(points)][1]) / 2,
        )
        for i in range(len(points))
    ]
    lat = sum(p[0] for p in points) / len(points)
    lng = sum(p[1] for p in points) / len(points)
    best = min(mids, key=lambda m: haversine_m(lat, lng, m[0], m[1]))
    return round(best[0], 6), round(best[1], 6)


def _bbox_circle(lat: float, lng: float, radius_m: float) -> tuple[float, float, float, float]:
    d_lat = radius_m / METERS_PER_DEG_LAT
    d_lng = radius_m / meters_per_deg_lng(lat)
    return lat - d_lat, lat + d_lat, lng - d_lng, lng + d_lng


def _category(value: Any) -> str:
    if value not in CATEGORY_NAMES:
        raise MarkingValidationError("请选择设施类别", "category")
    return str(value)


def _choice(value: Any, choices: dict[str, str], label: str) -> str:
    if value not in choices:
        raise MarkingValidationError(f"请选择{label}", label)
    return str(value)


def ttl_days(spec: dict[str, Any], requested: Any = None) -> int:
    """有效期天数：用户给了就校验范围，没给按类型取默认值。"""
    if requested is None:
        if spec["type"] == "closure":
            return CLOSURE_KINDS[spec["kind"]]
        return TYPE_TTL_DAYS[spec["type"]]
    if isinstance(requested, bool) or not isinstance(requested, int | float):
        raise MarkingValidationError("有效期应为天数", "expires_in_days")
    days = int(requested)
    if days != requested or not (MIN_TTL_DAYS <= days <= MAX_TTL_DAYS):
        raise MarkingValidationError(
            f"有效期应在 {MIN_TTL_DAYS}~{MAX_TTL_DAYS} 天之间", "expires_in_days"
        )
    return days


def normalize(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """校验并规范化一条标注。返回 (spec, columns)。

    spec 是权威内容（存库与存版本），columns 是由它推导出的查询字段。
    """
    typ = payload.get("type")
    if typ not in TYPES:
        raise MarkingValidationError("未知的标注类型", "type")
    note = clean_text(payload.get("note"), NOTE_MAX, "说明")
    spec: dict[str, Any] = {"type": typ, "note": note}
    geometry: dict[str, Any]
    category: str | None = None
    kind: str | None = None

    if typ == "closure":
        kind = payload.get("kind") or "construction"
        if kind not in CLOSURE_KINDS:
            raise MarkingValidationError("请选择围挡种类", "kind")
        lat, lng = check_point(payload.get("lat"), payload.get("lng"))
        radius = _number(payload.get("radius_m", 50), "半径")
        lo, hi = CLOSURE_RADIUS_M
        if not lo <= radius <= hi:
            raise MarkingValidationError(f"围挡半径应在 {lo:.0f}~{hi:.0f} 米之间", "radius_m")
        radius = round(radius, 1)
        spec.update(kind=kind, lat=lat, lng=lng, radius_m=radius)
        geometry = {"type": "Point", "coordinates": [lng, lat]}
        bbox = _bbox_circle(lat, lng, radius)
    elif typ in ("facility_missing", "facility_extra"):
        category = _category(payload.get("category"))
        name = clean_text(payload.get("name"), NAME_MAX, "设施名称", required=True)
        lat, lng = check_point(payload.get("lat"), payload.get("lng"), "设施位置")
        spec.update(category=category, name=name, lat=lat, lng=lng)
        if typ == "facility_missing":
            spec["reason"] = _choice(payload.get("reason"), MISSING_REASONS, "失效原因")
        geometry = {"type": "Point", "coordinates": [lng, lat]}
        bbox = _bbox_circle(lat, lng, 1.0)
    else:
        points = check_polygon(payload.get("polygon"))
        reason = _choice(payload.get("reason"), GRAY_REASONS, "缺失原因")
        raw_cats = payload.get("categories") or []
        if not isinstance(raw_cats, list | tuple):
            raise MarkingValidationError("请选择缺哪类设施", "categories")
        cats: list[str] = []
        for c in raw_cats:
            name = _category(c)
            if name not in cats:
                cats.append(name)
        if not cats:
            raise MarkingValidationError("请至少选择一类缺少的设施", "categories")
        if reason == "other" and not note:
            raise MarkingValidationError("选「其他原因」时，请用一句话说明", "note")
        lat, lng = polygon_anchor(points)
        ring = [[p[1], p[0]] for p in points]
        spec.update(
            polygon=ring,
            reason=reason,
            categories=[c for c in CATEGORY_NAMES if c in cats],
        )
        geometry = {"type": "Polygon", "coordinates": [[*ring, ring[0]]]}
        lats = [p[0] for p in points]
        lngs = [p[1] for p in points]
        bbox = (min(lats), max(lats), min(lngs), max(lngs))

    columns = {
        "type": typ,
        "kind": kind,
        "category": category,
        "geometry": geometry,
        "lat": lat,
        "lng": lng,
        "min_lat": bbox[0],
        "max_lat": bbox[1],
        "min_lng": bbox[2],
        "max_lng": bbox[3],
        "note": note,
    }
    return spec, columns


def merge_patch(spec: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """把修改合并进原内容。只接受该类型可改的字段，None 表示不改。"""
    allowed = EDITABLE[spec["type"]]
    unknown = [k for k, v in patch.items() if v is not None and k not in allowed]
    if unknown:
        raise MarkingValidationError(f"这类标注不能修改：{'、'.join(unknown)}", unknown[0])
    merged = dict(spec)
    for key in allowed:
        if patch.get(key) is not None:
            merged[key] = patch[key]
    return merged


def expires_at(now: datetime, days: int) -> datetime:
    return now + timedelta(days=days)


def now_beijing() -> datetime:
    return datetime.now(BEIJING).replace(microsecond=0)


def parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=BEIJING)


def title(spec: dict[str, Any]) -> str:
    """一行可读的标题，列表、报告、审核队列都用它。"""
    typ = spec["type"]
    if typ == "closure":
        return f"{CLOSURE_KIND_LABELS[spec['kind']]} · 半径 {spec['radius_m']:.0f} 米"
    if typ == "facility_missing":
        return f"{spec['name']} · {MISSING_REASONS[spec['reason']]}"
    if typ == "facility_extra":
        return f"补录{spec['category']} · {spec['name']}"
    return f"{'、'.join(spec['categories'])} · {GRAY_REASONS[spec['reason']]}"


def distance_to(spec: dict[str, Any], lat: float, lng: float) -> float:
    """点到标注几何的距离（米）：落在围挡圆或区域内为 0。"""
    typ = spec["type"]
    if typ == "closure":
        return max(0.0, haversine_m(lat, lng, spec["lat"], spec["lng"]) - spec["radius_m"])
    if typ != "gray_area":
        return haversine_m(lat, lng, spec["lat"], spec["lng"])
    ring = [(p[1], p[0]) for p in spec["polygon"]]
    if point_in_polygon(lat, lng, ring):
        return 0.0
    return distance_to_polyline_m((lat, lng), [*ring, ring[0]])


def labels() -> dict[str, Any]:
    """前端要用的全部枚举与文案，由后端下发，两边不会各写一份而对不上。"""
    return {
        "types": TYPE_LABELS,
        "closure_kinds": {
            k: {"label": CLOSURE_KIND_LABELS[k], "ttl_days": v} for k, v in CLOSURE_KINDS.items()
        },
        "missing_reasons": MISSING_REASONS,
        "gray_reasons": GRAY_REASONS,
        "type_ttl_days": TYPE_TTL_DAYS,
        "sources": SOURCE_LABELS,
        "categories": list(CATEGORY_NAMES),
        "limits": {
            "note_max": NOTE_MAX,
            "name_max": NAME_MAX,
            "closure_radius_m": list(CLOSURE_RADIUS_M),
            "polygon_max_vertices": POLYGON_MAX_VERTICES,
            "polygon_max_area_km2": POLYGON_MAX_AREA_M2 / 1e6,
            "ttl_days": [MIN_TTL_DAYS, MAX_TTL_DAYS],
        },
    }
