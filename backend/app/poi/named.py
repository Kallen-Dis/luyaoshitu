"""经店名与详情核对的补充召回；按分析中心索引，不存居民起点。"""

import time

from .fresh import FRESH


def index_path(client, center):
    return client._cache.key_for_point("poi_named_index", *center)


def recalled(client, center) -> list[dict]:
    records = client._cache.read(index_path(client, center), client._ttl()) or []
    return [p["record"] for p in records if time.time() - p.get("at", 0) <= client._ttl()]


async def lookup(client, query, center) -> tuple[list[dict], str | None]:
    city = await client.reverse_geocode(*center)
    if not city or not city.get("city"):
        return [], "无法确定分析中心所在城市，按店名检索未完成。"
    records = await client.named_pois(query, city["city"])
    if records is None:
        return [], "按店名检索或详情核对失败，请稍后重试。"
    # 输入可以是普通超市；购物中心、地名等联想不能冒充门店。
    records = [
        p
        for p in records
        if any(
            w in str(p.get("name", "")) + str((p.get("detail_info") or {}).get("tag", ""))
            for w in ("超市", "生鲜", "菜场", "菜市场", "农贸", "果蔬")
        )
    ]
    return records, None


def remember(client, center, records):
    path = index_path(client, center)
    old = client._cache.read(path, client._ttl()) or []
    valid = [p for p in old if time.time() - p.get("at", 0) <= client._ttl()]
    by_id = {p["record"].get("uid", p["record"]["name"]): p for p in valid}
    for record in records:
        by_id[record.get("uid", record["name"])] = {
            "record": record,
            "at": time.time(),
            "category": FRESH,
        }
    client._cache.write(path, list(by_id.values())[-50:])
