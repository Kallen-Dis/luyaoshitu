"""有覆盖声明的 BD09 水域边界；无数据绝不等于无水。路径折线不扩成桥面。"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

from shapely.geometry import Point, Polygon, box, mapping, shape
from shapely.ops import transform, unary_union

from ..config import PROJECT_ROOT
from .geometry import METERS_PER_DEG_LAT, meters_per_deg_lng


@dataclass
class GeographicDomain:
    center: tuple
    water: object
    coverage: object
    source: str
    version: str

    def xy_shape(self, geometry):
        lat, lng = self.center
        mpl = meters_per_deg_lng(lat)
        return transform(lambda x, y: ((x - lng) * mpl, (y - lat) * METERS_PER_DEG_LAT), geometry)

    def allowed(self, lat, lng):
        p = Point(lng, lat)
        return self.coverage.covers(p) and not self.water.covers(p)

    def clip(self, geometry):
        clipped = shape(geometry).intersection(self.coverage).difference(self.water)
        parts = [clipped] if clipped.geom_type == "Polygon" else list(clipped.geoms)
        faces = [
            mapping(p)["coordinates"] for p in parts if p.geom_type == "Polygon" and p.area > 0
        ]
        return {"type": "MultiPolygon", "coordinates": faces}

    def area(self, geometry):
        return self.xy_shape(shape(geometry)).area

    def metadata(self):
        return {
            "status": "covered",
            "source": self.source,
            "version": self.version,
            "coord_sys": "bd09",
            "bridge_policy": "仅保留数据中明确提供的步行桥面",
        }


def load_domain(center, radius_m, path: Path | None = None):
    """覆盖必须包含整个分析窗口；拒绝未知坐标系、无来源及无效环。"""
    path = path or Path(
        os.environ.get("GEOGRAPHIC_DOMAIN_PATH", PROJECT_ROOT / "data/geography/domain.geojson")
    )
    missing = {"status": "missing", "reason": "未安装该地区水域边界数据，范围仍为路网插值估计"}
    if not path.is_file():
        return None, missing
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        meta = data["properties"]
        if meta["coord_sys"] != "bd09" or not meta["source"] or not meta["version"]:
            raise ValueError("缺少坐标系、来源或版本")
        coverage = shape(meta["coverage"])
        features = data["features"]
        masks, decks = [], []
        for f in features:
            kind = f["properties"]["kind"]
            g = shape(f["geometry"])
            if g.geom_type not in ("Polygon", "MultiPolygon") or not g.is_valid:
                raise ValueError("水域或步行桥面几何无效")
            if kind == "water":
                masks.append(g)
            elif kind == "walkable_deck":
                decks.append(g)
            else:
                raise ValueError("未知地理要素类型")
        lat, lng = center
        dx, dy = radius_m / meters_per_deg_lng(lat), radius_m / METERS_PER_DEG_LAT
        if not coverage.is_valid or not coverage.covers(
            box(lng - dx, lat - dy, lng + dx, lat + dy)
        ):
            return None, {**missing, "status": "incomplete", "reason": "地理数据未完整覆盖分析窗口"}
        water = unary_union(masks).difference(unary_union(decks))
        domain = GeographicDomain(center, water, coverage, meta["source"], meta["version"])
        return domain, domain.metadata()
    except (ValueError, KeyError, TypeError, OSError) as exc:
        return None, {**missing, "status": "invalid", "reason": f"地理边界数据无效：{exc}"}


def mesh_domain(mesh, domain):
    """先沿水域边界分割三角网；跨水单元不参与残差耦合或等值线。"""
    water = domain.xy_shape(domain.water)
    bounded = water.intersection(box(-mesh.limit, -mesh.limit, mesh.limit, mesh.limit))
    parts = [bounded] if bounded.geom_type == "Polygon" else list(getattr(bounded, "geoms", []))
    for part in parts:
        if part.geom_type != "Polygon":
            continue
        for ring in [part.exterior, *part.interiors]:
            coords = list(ring.coords)
            for a, b in zip(coords, coords[1:], strict=False):
                mesh.conform_segment(a, b)
    for i, face in enumerate(mesh.faces):
        if face is not None:
            p = Polygon([mesh.points[k] for k in face])
            if p.intersection(water).area > 1e-6:
                mesh.faces[i] = None
