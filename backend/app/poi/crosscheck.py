"""AI 二次核对：用百度地图 Agent Plan 的语义地点检索，给关键设施做「第二次召回」。

项目的设施来自地点检索（place/v2/search）按关键词召回。漏召回会凭空造出盲区：
「这里没有小学」可能只是关键词没查到。Agent Plan 的地点检索是另一条通道，按自然语言
理解需求、按距离排序，召回口径不同，正好拿来交叉核对。

对每片灰色区域、每个缺的关键品类，以区域锚点为中心问一次「附近最近的 X」，把返回的设施
和结果里同类设施逐个比对（同名 500 米内或相距 100 米内算同一家）：

- **已收录**：两条通道都找到了，结论有交叉印证；
- **未收录、直线 1 公里内有缺这一类的方格**：疑似漏收录，列为线索，由人确认；
- **未收录、离所有缺口方格都超过 1 公里**：步行只会更远，不影响结论。

只给线索，不改结论。Agent Plan 按调用扣额度、文档要求相同参数不要重复请求，所以每个问题的
回答都缓存在 .cache/agent_plan/（缓存键与 scripts/crosscheck_poi.py 相同，开发期问过的直接命中）。
Agent Plan 的坐标是 GCJ02，这里按百度公开的 BD09 ↔ GCJ02 公式在本地换算（误差在米级）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any

import httpx

from ..isochrone.geometry import haversine_m
from ..report.regions import region_labels
from .catalog import by_name
from .collect import normalize_name

BASE_URL = "https://api.map.baidu.com/agent_plan/v1"
WALK_LIMIT_M = 1000.0
SAME_PLACE_M = 100.0  # 同类设施相距这么近，视为同一家（两条通道的坐标口径略有差异）
SAME_NAME_M = 500.0  # 归一化后同名、相距这么近，也算同一家
# 一次点击最多问几个问题：大片区域缺三类时也就 3 个，样例最多 3 个
MAX_QUESTIONS = 8

# 每个品类怎么问。保留完整的需求句子，不压成关键词（Agent Plan 的使用准则）
QUESTIONS = {
    "生鲜采买": "帮我找附近最近的菜市场、农贸市场或生鲜超市",
    "医药": "帮我找附近最近的药店",
    "基础教育": "帮我找附近最近的小学",
}

# 每类认哪些「二级分类」。名字里带「小学」的托管班、学校的停车场靠这个挡掉
LABELS = {
    "生鲜采买": {"市场", "菜市场", "农贸市场"},
    "医药": {"药店"},
    "基础教育": {"小学"},
}

# ---------- BD09 ↔ GCJ02（百度公开的换算公式） ----------

X_PI = math.pi * 3000.0 / 180.0


def bd09_to_gcj02(lat: float, lng: float) -> tuple[float, float]:
    x, y = lng - 0.0065, lat - 0.006
    z = math.hypot(x, y) - 0.00002 * math.sin(y * X_PI)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * X_PI)
    return z * math.sin(theta), z * math.cos(theta)


def gcj02_to_bd09(lat: float, lng: float) -> tuple[float, float]:
    z = math.hypot(lng, lat) + 0.00002 * math.sin(lat * X_PI)
    theta = math.atan2(lat, lng) + 0.000003 * math.cos(lng * X_PI)
    return z * math.sin(theta) + 0.006, z * math.cos(theta) + 0.0065


# ---------- Agent Plan 客户端 ----------


class AgentPlanError(RuntimeError):
    """一次提问失败。fatal 为真时（没配 Token、Token 无效）后面的问题也不必再问。"""

    def __init__(self, message: str, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal


def cache_path(cache_dir: Path, endpoint: str, params: dict[str, str]) -> Path:
    raw = json.dumps([endpoint, sorted(params.items())], ensure_ascii=False)
    return (
        cache_dir / "agent_plan" / f"{endpoint}-{hashlib.sha1(raw.encode()).hexdigest()[:16]}.json"
    )


class AgentPlanClient:
    """只读缓存、串行提问、记数。成功的回答才缓存，失败的下次还能重问。"""

    def __init__(
        self,
        token: str | None,
        cache_dir: Path,
        transport: httpx.AsyncBaseTransport | None = None,
        pause_s: float = 0.5,
        timeout_s: float = 40.0,
    ) -> None:
        self._token = token
        self._cache_dir = cache_dir
        self._pause = pause_s
        self._http = httpx.AsyncClient(timeout=timeout_s, transport=transport)
        self.requests = 0
        self.cache_hits = 0

    async def __aenter__(self) -> AgentPlanClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._http.aclose()

    async def get(self, endpoint: str, params: dict[str, str]) -> dict[str, Any]:
        path = cache_path(self._cache_dir, endpoint, params)
        if path.exists():
            try:
                body = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                body = None
            if isinstance(body, dict):
                self.cache_hits += 1
                return body
        if not self._token:
            raise AgentPlanError("没有配置 BAIDU_MAP_AUTH_TOKEN（Agent Plan 的 Token）", fatal=True)

        last: Exception | None = None
        resp: httpx.Response | None = None
        for attempt in range(2):  # 网络抖动重试一次；业务错误不重试，避免重复扣额度
            try:
                resp = await self._http.get(
                    f"{BASE_URL}/{endpoint}",
                    params=params,
                    headers={"Authorization": f"Bearer {self._token}"},
                )
                self.requests += 1
                break
            except httpx.HTTPError as exc:
                last = exc
                await asyncio.sleep(1.0 * (attempt + 1))
        if resp is None:
            raise AgentPlanError(f"连不上 Agent Plan：{type(last).__name__}")
        if resp.status_code in (401, 403):
            raise AgentPlanError(
                f"Agent Plan 拒绝了 Token（HTTP {resp.status_code}），请检查 BAIDU_MAP_AUTH_TOKEN",
                fatal=True,
            )
        if resp.status_code == 429:
            raise AgentPlanError("Agent Plan 限流（HTTP 429），请稍后再试", fatal=True)
        if resp.status_code != 200:
            raise AgentPlanError(f"Agent Plan 请求失败（HTTP {resp.status_code}）")
        try:
            body = resp.json()
        except ValueError as exc:
            raise AgentPlanError("Agent Plan 返回的内容无法解析") from exc
        status = int(body.get("status", -1)) if isinstance(body, dict) else -1
        if status != 0:
            message = str((body or {}).get("message") or (body or {}).get("msg") or "")
            raise AgentPlanError(
                f"Agent Plan 返回错误（status {status}）{'：' + message if message else ''}"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        if self._pause:
            await asyncio.sleep(self._pause)  # 慢慢问，不贴着限流
        return body


class RateLimited(RuntimeError):
    pass


class HourlyQuota:
    """进程级滑动窗口：一小时内实发的 Agent Plan 问题数封顶。命中缓存的不计。"""

    def __init__(self, limit: int, window_s: float = 3600.0) -> None:
        self.limit = limit
        self.window_s = window_s
        self._spent: deque[float] = deque()
        self._lock = threading.Lock()

    def _trim(self, now: float) -> None:
        while self._spent and now - self._spent[0] > self.window_s:
            self._spent.popleft()

    def take(self) -> None:
        """提问前检查：额度已满就拒绝。"""
        with self._lock:
            self._trim(time.monotonic())
            if len(self._spent) >= self.limit:
                raise RateLimited(
                    f"AI 二次核对这一小时已经问了 {self.limit} 个新问题，请稍后再试"
                    "（问过的问题直接读缓存，不受影响）。"
                )

    def spend(self, n: int) -> None:
        with self._lock:
            now = time.monotonic()
            self._spent.extend([now] * max(0, n))
            self._trim(now)


# ---------- 从结果里取出「缺口」 ----------


@dataclass
class Gap:
    """一片灰色区域里缺某一类的方格。"""

    region: str
    category: str
    anchor: tuple[float, float]  # BD09
    cells: list[tuple[float, float]]
    supply: int
    barrier: int


def gaps_of(feature: dict[str, Any]) -> list[Gap]:
    """每片编了号的灰色区域 × 每个缺的关键品类。格子多的排前面，问题数封顶时先问大片的。"""
    p = feature.get("properties") or {}
    blind = p.get("blindspots") or {}
    cells = blind.get("cells") or []
    regions = ((p.get("report") or {}).get("gray_regions") or {}).get("regions") or []
    labels = region_labels(blind)
    gaps: list[Gap] = []
    for region in regions:
        rid = region.get("id")
        anchor = region.get("anchor") or {}
        if not rid or "lat" not in anchor or "lng" not in anchor:
            continue
        for diag in region.get("diagnosis") or []:
            cat = diag.get("category")
            if cat not in QUESTIONS:
                continue
            in_region = [
                (c["lat"], c["lng"])
                for i, c in enumerate(cells)
                if labels.get(i) == rid and cat in (c.get("missing") or [])
            ]
            if not in_region:
                continue
            gaps.append(
                Gap(
                    region=rid,
                    category=cat,
                    anchor=(float(anchor["lat"]), float(anchor["lng"])),
                    cells=in_region,
                    supply=int(diag.get("supply_cells") or 0),
                    barrier=int(diag.get("barrier_cells") or 0),
                )
            )
    gaps.sort(key=lambda g: -len(g.cells))
    return gaps


def places_by_category(feature: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for place in ((feature.get("properties") or {}).get("coverage") or {}).get("places") or []:
        if place.get("category") and "lat" in place and "lng" in place:
            out.setdefault(place["category"], []).append(place)
    return out


def question(gap: Gap, region_name: str) -> dict[str, str]:
    glat, glng = bd09_to_gcj02(*gap.anchor)
    return {
        "user_raw_request": QUESTIONS[gap.category],
        "region": region_name,
        "center": f"{glat:.6f},{glng:.6f}",
        "sort": "distance",
    }


def region_from_geocode(place: dict[str, str] | None) -> str | None:
    """逆地理编码 → Agent Plan 要的 region：「上海市普陀区」这样到区县为止的前缀。"""
    if not place:
        return None
    address = str(place.get("address") or "")
    district = str(place.get("district") or "")
    if district and district in address:
        return address[: address.index(district) + len(district)]
    return (str(place.get("city") or "") + district) or None


# ---------- 解析 Agent Plan 的回答 ----------
#
# 实测回答（2026-10-04）：{"status": 0, "results": [...], "resource_key": ...}，每条结果有
# name、location（GCJ02）、uid、detail_info.tag（如「教育培训;小学」）、
# detail_info.parent_id（子点：学校的停车场、东南 1 门……）。


def places_of(body: dict[str, Any] | None) -> list[dict[str, Any]]:
    out, seen = [], set()
    for item in (body or {}).get("results") or []:
        loc = item.get("location") or {}
        if "lat" not in loc or "lng" not in loc or not item.get("name"):
            continue
        lat, lng = gcj02_to_bd09(float(loc["lat"]), float(loc["lng"]))
        key = (item["name"], round(lat, 5), round(lng, 5))
        if key in seen:
            continue
        seen.add(key)
        info = item.get("detail_info") or {}
        tag = str(info.get("tag") or "")
        out.append(
            {
                "name": str(item["name"]),
                "lat": lat,
                "lng": lng,
                "uid": item.get("uid"),
                "tag": tag,
                "label": tag.split(";")[-1] if tag else str(info.get("label") or ""),
                "sub": bool(info.get("parent_id")),
            }
        )
    return out


def is_category(place: dict[str, Any], category: str) -> bool:
    """只留这一类的主点：子点（停车场、出入口）不算，名字带「小学」的托管班也不算。"""
    cat = by_name(category)
    if cat is None or place["sub"]:
        return False
    if any(word in place["name"] for word in cat.exclude):
        return False
    label = place["label"]
    if label in LABELS[category]:
        return True
    # 生鲜超市在百度的分类里是「购物;超市」，名字带生鲜、菜的才算
    return (
        category == "生鲜采买"
        and "超市" in label
        and any(w in place["name"] for w in ("生鲜", "菜", "农"))
    )


def matches(found: dict[str, Any], ours: list[dict[str, Any]]) -> dict[str, Any] | None:
    norm = normalize_name(found["name"])
    best, best_d = None, math.inf
    for p in ours:
        d = haversine_m(found["lat"], found["lng"], p["lat"], p["lng"])
        same = d <= SAME_PLACE_M or (normalize_name(p["name"]) == norm and d <= SAME_NAME_M)
        if same and d < best_d:
            best, best_d = p, d
    return best


def compare(gap: Gap, body: dict[str, Any], ours: list[dict[str, Any]]) -> dict[str, Any]:
    """一个问题的回答 → 这片区域这一类的核对结果。"""
    everything = places_of(body)
    found = []
    for f in everything:
        if not is_category(f, gap.category):
            continue
        near = min(
            (haversine_m(f["lat"], f["lng"], c[0], c[1]) for c in gap.cells), default=math.inf
        )
        hit = matches(f, ours)
        found.append(
            {
                **f,
                "matched": hit["name"] if hit else None,
                "nearest_gap_m": round(near) if math.isfinite(near) else None,
                "from_anchor_m": round(haversine_m(f["lat"], f["lng"], *gap.anchor)),
            }
        )
    return {"returned": len(everything), "found": found}


def to_review(row: dict[str, Any]) -> list[dict[str, Any]]:
    """未收录、且直线 1 公里内有缺这一类的方格：可能改变结论，要复核。"""
    return [
        f
        for f in row.get("found") or []
        if not f["matched"]
        and f["nearest_gap_m"] is not None
        and f["nearest_gap_m"] <= WALK_LIMIT_M
    ]


@dataclass
class CrosscheckResult:
    rows: list[dict[str, Any]] = field(default_factory=list)
    skipped: int = 0  # 超出问题数上限、没问的「区域 × 品类」
    aborted: str | None = None  # 致命错误（Token 无效等）中途停下的原因


async def crosscheck(
    feature: dict[str, Any],
    client: AgentPlanClient,
    region_name: str,
    only_region: str | None = None,
    limit: int | None = MAX_QUESTIONS,
) -> CrosscheckResult:
    """逐个问题核对。单个问题失败只记在那一行（写「没核对成功」，不当成「没有」）。"""
    gaps = gaps_of(feature)
    if only_region:
        gaps = [g for g in gaps if g.region == only_region]
    asked = gaps if limit is None else gaps[:limit]
    ours = places_by_category(feature)
    out = CrosscheckResult(skipped=len(gaps) - len(asked))
    for gap in asked:
        params = question(gap, region_name)
        row: dict[str, Any] = {"gap": gap, "asked": params, "error": None, "found": []}
        if out.aborted:
            row["error"] = out.aborted
            out.rows.append(row)
            continue
        try:
            body = await client.get("place", params)
        except AgentPlanError as exc:
            row["error"] = str(exc)
            if exc.fatal:
                out.aborted = str(exc)
            out.rows.append(row)
            continue
        row["status"] = int(body.get("status", -1))
        row.update(compare(gap, body, ours.get(gap.category, [])))
        out.rows.append(row)
    return out


def as_payload(
    result: CrosscheckResult, region_name: str, client: AgentPlanClient
) -> dict[str, Any]:
    """接口返回的形状：逐行核对结果 + 拉平的疑似漏收录清单。坐标一律 BD09。"""

    def place_out(f: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": f["name"],
            "lat": round(f["lat"], 6),
            "lng": round(f["lng"], 6),
            "matched": f["matched"],
            "nearest_gap_m": f["nearest_gap_m"],
            "from_anchor_m": f["from_anchor_m"],
        }

    rows, suspects = [], []
    for row in result.rows:
        gap: Gap = row["gap"]
        review = to_review(row)
        rows.append(
            {
                "region": gap.region,
                "category": gap.category,
                "cells": len(gap.cells),
                "supply_cells": gap.supply,
                "barrier_cells": gap.barrier,
                "error": row["error"],
                "returned": row.get("returned", 0),
                "found": [place_out(f) for f in row["found"]],
                "matched": sum(1 for f in row["found"] if f["matched"]),
                "suspects": len(review),
            }
        )
        for f in sorted(review, key=lambda x: x["nearest_gap_m"]):
            suspects.append({"region": gap.region, "category": gap.category, **place_out(f)})
    return {
        "region_name": region_name,
        "rows": rows,
        "suspects": suspects,
        "skipped": result.skipped,
        "aborted": result.aborted,
        "agent_plan": {"requests": client.requests, "cache_hits": client.cache_hits},
        "max_questions": MAX_QUESTIONS,
    }
