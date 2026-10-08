"""给旧快照补查新增召回词；只查分析中心，居民起点不进入地点磁盘缓存。"""

from ..isochrone.geometry import haversine_m, point_in_polygon
from ..poi.catalog import by_name
from ..poi.collect import DEDUP_GRID_M, clean, normalize_name
from ..poi.fresh import annotate_coverage, fields
from ..poi.named import lookup, recalled, remember
from .candidates import place_id, point
from .context import matches

FRESH_KEYWORDS = ("生鲜", "生鲜大卖场", "超市")


async def supplement(
    client, feature: dict, categories: list[str], poi_query: str | None = None
) -> list[str]:
    props = feature["properties"]
    if props.get("simulated") or "生鲜采买" not in categories:
        return []
    coverage = props["coverage"]
    if "生鲜采买" in coverage.get("failed_categories", []):
        return []  # 一次补查不足以把原来的失败品类改成成功。
    center = point(props["center"])
    radius = int(coverage["radius_m"])
    metadata = coverage.setdefault("search_metadata", {}).setdefault("生鲜采买", [])
    existing = {m.get("keyword") for m in metadata}
    warnings = []
    added = 0
    polygon = [(lat, lng) for lng, lat in feature["geometry"]["coordinates"][0]]
    excluded = [
        m.get("spec") or {}
        for m in (props.get("markings") or {}).get("applied", [])
        if m.get("type") == "facility_missing"
    ]
    batches = []
    named = recalled(client, center)
    if poi_query:
        records, error = await lookup(client, poi_query, center)
        if error:
            warnings.append(error)
        else:
            cleaned, _ = clean(records, by_name("生鲜采买"), center, radius)
            valid_names = {(p.raw_name, p.lat, p.lng) for p in cleaned}
            valid = [
                r
                for r in records
                if (r["name"], r["location"]["lat"], r["location"]["lng"]) in valid_names
            ]
            remember(client, center, valid)
            named.extend(valid)
            if not valid:
                warnings.append("没有找到检索范围内、名称匹配且经详情核对的买菜门店。")
            else:
                warnings.append(f"按店名与详情核对召回 {len(valid)} 家门店；商品信息仍按证据分级。")
    if named:
        batches.append(named)
    for keyword in FRESH_KEYWORDS:
        if keyword in existing:
            continue
        records = await client.search_poi_all(keyword, *center, radius, max_pages=6)
        if records is None:
            warnings.append(f"补充检索「{keyword}」失败，仍使用原设施快照；可能遗漏更近的店。")
            continue
        # 没有原始分页证据的旧快照不能仅因补查成功就宣称检索完整。
        if not metadata:
            metadata.append({"keyword": "旧快照检索", "complete": False})
        metadata.append(client.poi_metadata(keyword, *center, radius, 6))
        batches.append(records)
    for records in batches:
        pois, _ = clean(records, by_name("生鲜采买"), center, radius)
        places = coverage["places"]
        for poi in pois:
            if any(
                matches(
                    {"category": poi.category, "name": poi.name, "lat": poi.lat, "lng": poi.lng}, s
                )
                for s in excluded
            ):
                continue
            if any(
                p.get("category") == poi.category
                and normalize_name(str(p.get("name") or "")) == poi.name
                and haversine_m(poi.lat, poi.lng, p["lat"], p["lng"]) <= DEDUP_GRID_M
                for p in places
            ):
                continue
            if len(places) >= 2000:
                warnings.append("设施候选达到上限，补充检索未全部纳入。")
                break
            p = {
                "name": poi.name,
                "category": poi.category,
                "lat": round(poi.lat, 6),
                "lng": round(poi.lng, 6),
                "in_circle": point_in_polygon(poi.lat, poi.lng, polygon),
                **fields(poi),
            }
            p["id"] = place_id(p)
            places.append(p)
            added += 1
    if added:
        warnings.append(
            f"为当前出行补充检索到 {added} 家买菜候选；原快照的网格判定需要重新体检更新。"
        )
    annotate_coverage(coverage)
    return warnings
