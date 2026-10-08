"""买菜能力的证据分级；门店类型和品牌均不是商品库存证明。"""

FRESH = "生鲜采买"
VERSION = 3
ACCEPTED = {"verified", "inferred"}
# 仅列综合卖场业态；普通「联华」「华联」等品牌不在此列。
FORMATS = ("世纪联华", "大润发", "沃尔玛购物广场", "山姆会员商店", "麦德龙", "盒马鲜生")
VEGETABLE = ("菜市", "菜场", "农贸", "蔬菜", "果蔬", "菜店")
SPECIALTY = ("便利店", "便利超市", "水果", "果业", "零食", "烟酒", "肉铺", "肉店", "水产", "海鲜")
NON_GROCERY = ("驿站", "快递", "超市货架", "超市设备", "慈善超市")
IRRELEVANT = ("购物中心", "百货", "商场", "超市货架", "超市设备")


def classify(name: str, tag: str = "", status: str | None = None, evidence: str = "") -> dict:
    if status == "verified":
        return {
            "fresh_status": status,
            "fresh_evidence": evidence or "用户现场确认销售蔬菜",
            "fresh_rule_version": VERSION,
        }
    if any(word in name for word in ("超市货架", "超市设备")):
        state, reason = "excluded", "设备或货架商家，不作为买菜门店推荐"
    elif any(word in name for word in VEGETABLE):
        state, reason = "inferred", "名称包含菜场、农贸或蔬菜信息；商品未逐店核实"
    elif any(word in name for word in (*SPECIALTY, *NON_GROCERY)):
        state, reason = "excluded", "便利、驿站或专营业态，默认不作为买菜候选；现场确认可补录"
    elif "生鲜" in name:
        state, reason = "inferred", "名称明确包含生鲜；是否销售蔬菜仍为规则推定"
    elif any(word in name for word in FORMATS):
        state, reason = "inferred", "名称匹配综合卖场业态白名单；未核实该分店商品"
    elif any(word in name for word in IRRELEVANT) and "超市" not in name:
        state, reason = "excluded", "商场或购物中心本身不算买菜门店"
    else:
        state, reason = "pending", "普通超市或名称信息不足；是否卖菜待确认"
    return {"fresh_status": state, "fresh_evidence": reason, "fresh_rule_version": VERSION}


def fields(place) -> dict:
    """兼容 POI 对象与旧快照；旧记录不得自动升级为已核实。"""
    get = (
        place.get
        if isinstance(place, dict)
        else lambda key, default=None: getattr(place, key, default)
    )
    if get("category") != FRESH:
        return {}
    return classify(
        str(get("name", "")),
        str(get("tag", "")),
        get("fresh_status"),
        str(get("fresh_evidence", "") or ""),
    )


def eligible(place) -> bool:
    return not fields(place) or fields(place)["fresh_status"] in ACCEPTED


def annotate_coverage(coverage: dict) -> None:
    places = coverage.get("places")
    if places is None or FRESH in coverage.get("failed_categories", []):
        return
    fresh = [p for p in places if p.get("category") == FRESH]
    if not fresh and FRESH not in coverage.get("categories", {}):
        return  # 没采这一品类，不能凭空制造「零家」的覆盖结论。
    for p in fresh:
        p.update(fields(p))
    breakdown = {
        s: sum(p["fresh_status"] == s for p in fresh)
        for s in ("verified", "inferred", "pending", "excluded")
    }
    breakdown["pending_in_circle"] = sum(
        p["fresh_status"] == "pending" and p.get("in_circle", True) for p in fresh
    )
    coverage["fresh_breakdown"] = breakdown
    coverage.setdefault("categories", {})[FRESH] = sum(
        eligible(p) and p.get("in_circle", True) for p in fresh
    )
    coverage.setdefault("nearby_categories", {})[FRESH] = sum(eligible(p) for p in fresh)
