"""快照入口状态、经验下界与范围口径；不请求百度。"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from ..isochrone.geometry import haversine_m
from ..poi.fresh import eligible, fields


class TripError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def point(raw: dict) -> tuple[float, float]:
    try:
        lat, lng = float(raw["lat"]), float(raw["lng"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TripError("invalid", "起点和设施必须具有有效的 BD09 坐标。") from exc
    if not (math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180):
        raise TripError("invalid", "坐标不是有效的经纬度。")
    return round(lat, 6), round(lng, 6)


def place_id(place: dict) -> str:
    lat, lng = point(place)
    raw = f"{place.get('category','')}|{place.get('name','')}|{lat:.6f}|{lng:.6f}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


@dataclass(frozen=True)
class Node:
    id: str
    place_id: str
    name: str
    category: str
    lat: float
    lng: float
    gate: str | None = None
    distance_basis: str = "coordinate"
    in_circle: bool = False
    fresh_status: str | None = None
    fresh_evidence: str | None = None

    @property
    def coord(self) -> tuple[float, float]:
        return self.lat, self.lng

    def as_dict(self) -> dict:
        return {
            "entry_id": self.id,
            "place_id": self.place_id,
            "name": self.name,
            "category": self.category,
            "lat": self.lat,
            "lng": self.lng,
            "gate": self.gate,
            "distance_basis": self.distance_basis,
            "in_circle": self.in_circle,
            **(
                {"fresh_status": self.fresh_status, "fresh_evidence": self.fresh_evidence}
                if self.fresh_status
                else {}
            ),
        }


def distance(a: Node, b: Node) -> float:
    return haversine_m(a.lat, a.lng, b.lat, b.lng)


def lower_bound(a: Node, b: Node, slack: float) -> float:
    return max(0.0, distance(a, b) - slack)


def blocked(coord: tuple[float, float], closures: list[dict]) -> bool:
    return any(haversine_m(*coord, *point(c)) <= float(c["radius_m"]) for c in closures)


def candidates(
    feature: dict, category: str, closures: list[dict], include_pending: bool = False
) -> list[Node]:
    coverage = feature["properties"].get("coverage") or {}
    if category in coverage.get("failed_categories", []):
        raise TripError("category_failed", "这一类检索失败，设施数量未知，请重新检索。")
    out: dict[str, Node] = {}
    for p in coverage.get("places", []):
        if p.get("category") != category:
            continue
        fresh = fields(p)
        if fresh and (
            fresh["fresh_status"] == "excluded" or (not include_pending and not eligible(p))
        ):
            continue
        pid = place_id(p)
        raw_entries = p.get("entries") or [
            {"lat": p["lat"], "lng": p["lng"], "basis": "coordinate"}
        ]
        for e in raw_entries:
            lat, lng = point(e)
            if blocked((lat, lng), closures):
                continue
            name = str(e.get("name") or "")
            basis = e.get("basis") or (
                "navigation_point" if name == "导航点" else "gate" if name else "coordinate"
            )
            eid = f"{pid}:{lat:.6f}:{lng:.6f}"
            out[eid] = Node(
                eid,
                pid,
                str(p.get("name") or "未命名设施"),
                category,
                lat,
                lng,
                name or None,
                basis,
                bool(p.get("in_circle")),
                fresh.get("fresh_status"),
                fresh.get("fresh_evidence"),
            )
    return list(out.values())


def prepare(feature: dict, raw_origin: dict) -> tuple[Node, float, list[dict]]:
    props = feature.get("properties") or {}
    coverage = props.get("coverage") or {}
    if "places" not in coverage:
        raise TripError("no_places", "这份结果没有设施列表，请打开设施覆盖重新计算。")
    if len(coverage.get("places") or []) > 2000:
        raise TripError("invalid", "设施列表超过单次规划上限。")
    center = point(props.get("center") or {})
    lat, lng = point(raw_origin)
    try:
        radius = float(coverage.get("radius_m") or 2500)
    except (TypeError, ValueError) as exc:
        raise TripError("invalid", "设施检索半径无效。") from exc
    if not math.isfinite(radius) or not 0 < radius <= 20000:
        raise TripError("invalid", "设施检索半径无效。")
    coverage["radius_m"] = radius
    from_center = haversine_m(lat, lng, *center)
    if from_center > radius:
        raise TripError("origin_out_of_range", "起点离分析中心太远，请先在那里体检一次。")
    from .context import closures_from_feature

    closures = closures_from_feature(feature)
    if len(closures) > 20:
        raise TripError("invalid", "围挡数量超过上限。")
    for c in closures:
        c["lat"], c["lng"] = point(c)
        try:
            r = float(c.get("radius_m", 0))
        except (TypeError, ValueError) as exc:
            raise TripError("invalid", "围挡半径无效。") from exc
        if not math.isfinite(r) or not 0 < r <= 300:
            raise TripError("invalid", "围挡半径无效。")
        c["radius_m"] = r
    return Node("origin", "origin", "起点", "", lat, lng), from_center, closures


def search_status(feature: dict, categories: list[str]) -> str:
    coverage = feature["properties"].get("coverage") or {}
    if any(c in coverage.get("failed_categories", []) for c in categories):
        return "failed"
    metadata = coverage.get("search_metadata") or {}
    pages = [p for c in categories for p in metadata.get(c, [])]
    if not pages or any(not metadata.get(c) for c in categories):
        return "unknown"
    if any(p.get("truncated") for p in pages):
        return "truncated"
    return "no_truncation_observed" if all(p.get("complete") for p in pages) else "unknown"
