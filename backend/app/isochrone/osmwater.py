"""按区域缓存 OSM 原始水域数据；断裂海岸线、残缺关系、超时均不算无水。"""

import asyncio
import hashlib
import json
import math
import os
import time
from pathlib import Path

import httpx
from shapely.errors import ShapelyError
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import polygonize_full, transform, unary_union
from shapely.strtree import STRtree

from ..config import PROJECT_ROOT
from .domain import GeographicDomain
from .geometry import METERS_PER_DEG_LAT, meters_per_deg_lng
from .osmcoords import to_bd, to_wgs

FORMAT = "osm-water-v1.5"
TTL = 30 * 86400
_lock = asyncio.Lock()
_last_request = 0.0
ENDPOINT = "https://maps.mail.ru/osm/tools/overpass/api/interpreter"


def region(center, radius):
    lat, lng = to_wgs(*center)
    y, x = math.floor(lat / 0.02) * 0.02, math.floor(lng / 0.02) * 0.02
    radius = math.ceil(radius / 1000) * 1000 + 500
    dy, dx = radius / METERS_PER_DEG_LAT, radius / meters_per_deg_lng(y + 0.01)
    return (x - dx, y - dy, x + 0.02 + dx, y + 0.02 + dy)


def query(bounds):
    x0, y0, x1, y1 = bounds
    b = f"({y0:.7f},{x0:.7f},{y1:.7f},{x1:.7f})"
    return (
        "[out:json][timeout:20][maxsize:134217728];("
        f'way["natural"="coastline"]{b};'
        f'nwr["natural"="water"]{b};nwr["waterway"="riverbank"]{b};'
        f'nwr["landuse"="reservoir"]{b};'
        f'way["man_made"="bridge"]["foot"="yes"]["area"="yes"]{b};'
        ");out geom;"
    )


def coordinates(item):
    data = item.get("geometry")
    if not data or any(p is None or "lat" not in p or "lon" not in p for p in data):
        raise ValueError("OSM 要素几何缺失")
    pts = [(float(p["lon"]), float(p["lat"])) for p in data]
    if len(pts) < 2 or any(not math.isfinite(v) for p in pts for v in p):
        raise ValueError("OSM 坐标无效")
    return pts


def rings(lines):
    polygons, cuts, dangles, invalid = polygonize_full(unary_union(lines))
    if cuts.length > 1e-9 or dangles.length > 1e-9 or not invalid.is_empty:
        raise ValueError("OSM 水域关系没有闭合，拒绝用直线补齐")
    return list(polygons.geoms)


def relation_polygon(item):
    groups = {"outer": [], "inner": []}
    for member in item.get("members", []):
        role = member.get("role") or "outer"
        if member.get("type") == "way" and role in groups:
            groups[role].append(LineString(coordinates(member)))
    if not groups["outer"]:
        raise ValueError("OSM 水域关系缺少外环")
    outer = unary_union(rings(groups["outer"]))
    inner = unary_union(rings(groups["inner"])) if groups["inner"] else Polygon()
    if not inner.is_empty and not outer.covers(inner):
        raise ValueError(f"OSM 水域关系{item.get('id')}内环超出外环")
    return outer.difference(inner)


def sea_polygon(coasts, frame):
    if not coasts:
        return Polygon()
    clipped = unary_union([line.intersection(frame) for line in coasts])
    faces = rings([frame.boundary, clipped])
    tree = STRtree(faces)
    labels = [set() for _ in faces]
    # OSM 海岸方向规则：陆地在左、水在右。保留原始 way 方向，不依赖 polygonize 的环方向。
    for line in coasts:
        for a, b in zip(line.coords, list(line.coords)[1:], strict=False):
            dx, dy = b[0] - a[0], b[1] - a[1]
            length = math.hypot(dx, dy)
            if length < 1e-12:
                continue
            mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            eps = min(1e-6, length / 100)
            for sign, label in ((1, "land"), (-1, "water")):
                p = Point(mid[0] - sign * dy / length * eps, mid[1] + sign * dx / length * eps)
                for index in tree.query(p, predicate="within"):
                    labels[int(index)].add(label)
    if any(len(s) != 1 for s in labels):
        raise ValueError("海岸线方向冲突或有未能判明海陆的片区")
    return unary_union([p for p, label in zip(faces, labels, strict=True) if "water" in label])


def build_domain(data, bounds, center):
    if (
        data.get("remark")
        or "elements" not in data
        or not data.get("osm3s", {}).get("timestamp_osm_base")
    ):
        raise ValueError("Overpass 响应不完整或没有源数据时间戳")
    frame = box(*bounds)
    elements = data["elements"]
    relations = [e for e in elements if e.get("type") == "relation"]
    members = {m["ref"] for r in relations for m in r.get("members", []) if m.get("type") == "way"}
    waters = [relation_polygon(r) for r in relations]
    coasts, decks = [], []
    for e in elements:
        if e.get("type") != "way":
            continue
        tags = e.get("tags", {})
        pts = coordinates(e)
        if tags.get("natural") == "coastline":
            coasts.append(LineString(pts))
            continue
        if e["id"] in members:
            continue
        if pts[0] != pts[-1] or len(pts) < 4:
            raise ValueError("OSM 水面/桥面 way 没有闭合")
        polygon = Polygon(pts)
        if not polygon.is_valid:
            raise ValueError("OSM 水面/桥面自交或几何无效")
        if tags.get("man_made") == "bridge":
            if tags.get("foot") == "yes" and tags.get("area") == "yes":
                decks.append(polygon)
        else:
            waters.append(polygon)
    waters.append(sea_polygon(coasts, frame))
    water = unary_union(waters).intersection(frame).difference(unary_union(decks))
    if not water.is_valid:
        raise ValueError("OSM 合并水域无效")
    source = "© OpenStreetMap contributors · ODbL 1.0 · Overpass"
    domain = GeographicDomain(
        center,
        transform(to_bd, water),
        transform(to_bd, frame),
        source,
        data["osm3s"]["timestamp_osm_base"],
    )
    if not domain.water.is_valid or not domain.coverage.is_valid:
        raise ValueError("坐标转换后的水域边界无效")
    return domain, {
        **domain.metadata(),
        "provider": FORMAT,
        "coastline_ways": len(coasts),
        "water_features": len(elements),
        "coastline_state": "reconstructed" if coasts else "not_in_query",
        "alignment": "local_approximation",
        "alignment_verified": False,
        "scope": "仅表示OSM已收录水域；不证明未收录处没有水",
        "license_url": "https://www.openstreetmap.org/copyright",
    }


async def resolve_osm_domain(center, radius, *, cache_dir=None, transport=None):
    """一次请求、30天区域缓存；失败冷却5分钟；过期不冒充新鲜覆盖。"""
    global _last_request
    bounds = region(center, radius)
    root = Path(cache_dir or PROJECT_ROOT / ".cache/osm-water")
    key = hashlib.sha256(json.dumps([FORMAT, bounds]).encode()).hexdigest()[:24]
    path, failure = root / f"{key}.json", root / f"{key}.failure.json"
    unavailable = {
        "status": "unavailable",
        "provider": FORMAT,
        "reason": "OSM 水域获取或重建未完成，不能据此判断没有水",
    }
    async with _lock:
        try:
            if path.exists() and time.time() - path.stat().st_mtime < TTL:
                payload = json.loads(path.read_text(encoding="utf8"))
                domain, meta = build_domain(payload, bounds, center)
                return domain, {
                    **meta,
                    "cache_hit": True,
                    "data_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            if failure.exists() and time.time() - failure.stat().st_mtime < 300:
                if path.exists() and time.time() - path.stat().st_mtime <= TTL * 3:
                    domain, meta = build_domain(
                        json.loads(path.read_text(encoding="utf8")), bounds, center
                    )
                    return domain, {
                        **meta,
                        "cache_hit": True,
                        "stale": True,
                        "refresh_error": "冷却中",
                    }
                return None, {
                    **unavailable,
                    "reason": "OSM 上次请求失败，5分钟冷却中；范围未完成水域核验",
                }
            endpoint = os.environ.get("OSM_OVERPASS_URL", ENDPOINT)
            if not endpoint.startswith("https://"):
                raise ValueError("OSM 服务必须使用 HTTPS")
            delay = 10 - (time.monotonic() - _last_request)
            if delay > 0:
                await asyncio.sleep(delay)
            _last_request = time.monotonic()
            endpoints = [endpoint]
            if "OSM_OVERPASS_URL" not in os.environ:
                endpoints.append("https://overpass-api.de/api/interpreter")
            attempts = []
            async with asyncio.timeout(28):
                for index, service in enumerate(endpoints):
                    try:
                        async with (
                            httpx.AsyncClient(
                                timeout=12,
                                transport=transport,
                                headers={"User-Agent": "Luyaoshitu/1.0 (OSM water cache)"},
                            ) as net,
                            net.stream("GET", service, params={"data": query(bounds)}) as response,
                        ):
                            response.raise_for_status()
                            body = bytearray()
                            async for chunk in response.aiter_bytes():
                                body.extend(chunk)
                                if len(body) > 16 * 1024 * 1024:
                                    raise ValueError("OSM 响应超过16MiB上限")
                        break
                    except httpx.HTTPError as exc:
                        attempts.append(type(exc).__name__)
                        # 限流不换节点绕过；只对超时及服务端故障使用一个备用服务。
                        retryable = (
                            not isinstance(exc, httpx.HTTPStatusError)
                            or exc.response.status_code >= 500
                        )
                        if not retryable or index == len(endpoints) - 1:
                            raise
            payload = json.loads(body)
            audit = root / "raw"
            audit.mkdir(parents=True, exist_ok=True)
            (audit / f"{key}.json").write_bytes(body)
            domain, meta = build_domain(payload, bounds, center)
            # 必须覆盖请求窗口，缓存的是原始完整响应，原子替换，取消不留下半文件。
            lat, lng = center
            dx, dy = radius / meters_per_deg_lng(lat), radius / METERS_PER_DEG_LAT
            if not domain.coverage.covers(box(lng - dx, lat - dy, lng + dx, lat + dy)):
                raise ValueError("OSM 重建结果未覆盖分析窗口")
            root.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(body)
            tmp.replace(path)
            return domain, {
                **meta,
                "cache_hit": False,
                "service_attempts": len(attempts) + 1,
                "data_sha256": hashlib.sha256(body).hexdigest(),
            }
        except (
            httpx.HTTPError,
            TimeoutError,
            ValueError,
            KeyError,
            TypeError,
            OSError,
            ShapelyError,
        ) as exc:
            root.mkdir(parents=True, exist_ok=True)
            detail = type(exc).__name__
            if isinstance(exc, httpx.HTTPStatusError):
                detail += f": HTTP {exc.response.status_code}"
            elif not isinstance(exc, httpx.HTTPError):
                detail += ": " + str(exc)[:250]
            failure.write_text(json.dumps({"reason": detail}), encoding="utf8")
            if path.exists() and time.time() - path.stat().st_mtime <= TTL * 3:
                try:
                    domain, meta = build_domain(
                        json.loads(path.read_text(encoding="utf8")), bounds, center
                    )
                    return domain, {
                        **meta,
                        "cache_hit": True,
                        "stale": True,
                        "refresh_error": detail,
                        "data_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                except (ValueError, KeyError, TypeError, OSError, ShapelyError):
                    pass
            return None, {**unavailable, "detail": detail}
