"""样例社区选址：用真实 POI 覆盖数据挑选演示用的目标社区。

选址标准不是"设施越多越好"，而是"内部落差越大越好"——需要一个既有配套完善区块、
又有明显服务盲区的社区，才能同时展示覆盖统计与盲区识别两项能力。
全覆盖的成熟城区跑不出盲区，纯荒地又显不出算法分辨力。

**两阶段渐进筛选**（为节省地点检索的日配额而设计）：
  阶段一：仅测各候选中心点，粗筛出"有设施但不齐全"的候选；
  阶段二：只对进入决赛的少数候选做四向探点，测内部均衡度。

一次性对所有候选做全量精细测量要 500 次检索；两阶段约 180 次，
且对明显不合格的候选不做无谓的精细测量。

用法：
    python scripts/pick_sample_area.py
    python scripts/pick_sample_area.py --finalists 3   # 进入阶段二的候选数
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from _singleton import AlreadyRunning, single_instance

ROOT = Path(__file__).resolve().parent.parent
BASE = "https://api.map.baidu.com"
REPORT_PATH = ROOT / "reports" / "sample-area.md"
CACHE_DIR = ROOT / ".cache" / "poi"

# 探测半径：1 公里，对应命题中「周边 1 公里内无菜市场/药店/小学即为盲区」的判定口径
RADIUS_M = 1000

# 民生设施分类。每类只留最具代表性的关键词以节省配额；
# 生鲜采买保留两个，因为上海本地"菜市场/菜场/生鲜超市"命名差异大，单关键词漏检风险高。
CATEGORIES: dict[str, list[str]] = {
    "生鲜采买": ["菜市场", "生鲜超市"],
    "医药": ["药店"],
    "基础医疗": ["社区卫生服务中心"],
    "基础教育": ["小学"],
    "养老服务": ["养老院"],
    "文体休闲": ["公园"],
}
CALLS_PER_POINT = sum(len(v) for v in CATEGORIES.values())


@dataclass
class Candidate:
    """候选社区。坐标在运行时由地理编码接口解析，换城市只需改名字列表。

    地理编码的日配额远比地点检索宽裕（实测地点检索耗尽时它仍正常），
    因此这点额外调用不构成负担，换来的是免去手工查坐标与坐标系转换的麻烦。
    """

    name: str
    note: str
    lat: float = 0.0
    lng: float = 0.0
    center: dict[str, int | None] = field(default_factory=dict)
    probes: list[dict[str, int | None]] = field(default_factory=list)


# 候选社区：上海市普陀区全部街道与镇。
# 覆盖「老工人新村 / 内环成熟区 / 副中心开发区 / 外环外转型区 / 新建居住区」多种形态，
# 横向跑完后择优，而不是一开始就凭印象锁死一个。
CITY_PREFIX = "上海市普陀区"
CANDIDATES = [
    Candidate("曹杨新村街道", "全国首个工人新村，配套成熟密集"),
    Candidate("长寿路街道", "内环内成熟商业居住区"),
    Candidate("长风新村街道", "长风生态商务区，产居混合"),
    Candidate("真如镇街道", "城市副中心，开发建设中"),
    Candidate("万里街道", "2000 年后新建大型居住区"),
    Candidate("长征镇", "中环外大型居住区"),
    Candidate("桃浦镇", "外环外老工业区转型，配套相对薄弱"),
    Candidate("甘泉路街道", "老旧居住区，人口密度高"),
    Candidate("宜川路街道", "苏州河北岸老旧居住区"),
    Candidate("石泉路街道", "老旧居住区，旧改推进中"),
]

# 内部均衡度探点：中心点向东南西北各偏移 800 米。
# 按米换算而非写死经纬度增量——经度方向的度距随纬度收缩，
# 北京（39.9°N）与上海（31.2°N）差了约 10%，写死会让不同城市的探点距离不一致。
PROBE_OFFSET_M = 800.0
METERS_PER_DEG_LAT = 111_320.0

# 401/402 是瞬时并发噪声，退避后可恢复。
# 302 则是按接口独立计算的「当日配额耗尽」，耗尽后持续返回，重试只会加速消耗，
# 必须立即终止（详见 reports/quota-report.md 第四节）。
RETRYABLE = {401, 402, 1}
QUOTA_EXHAUSTED = {301, 302}
MAX_RETRIES = 3
MAX_QPS = 8


class QuotaExhausted(RuntimeError):
    """地点检索当日配额耗尽。配额次日 0 点重置，届时重跑即可续上缓存。"""


def probe_offsets(lat: float) -> list[tuple[float, float]]:
    dlat = PROBE_OFFSET_M / METERS_PER_DEG_LAT
    dlng = PROBE_OFFSET_M / (METERS_PER_DEG_LAT * math.cos(math.radians(lat)))
    return [(dlat, 0.0), (-dlat, 0.0), (0.0, dlng), (0.0, -dlng)]


class RateLimiter:
    """令牌桶限流。探测脚本与正式后端共用同一套限速逻辑。"""

    def __init__(self, qps: int) -> None:
        self._interval = 1.0 / qps
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next - now)
            self._next = max(now, self._next) + self._interval
        if wait:
            await asyncio.sleep(wait)


class Budget:
    """统计实际发出的检索次数，让配额消耗可见、可复盘。"""

    def __init__(self) -> None:
        self.api_calls = 0
        self.cache_hits = 0

    def summary(self) -> str:
        total = self.api_calls + self.cache_hits
        rate = f"{self.cache_hits / total:.0%}" if total else "—"
        return f"地点检索 {self.api_calls} 次实发，{self.cache_hits} 次命中缓存（命中率 {rate}）"


def load_ak() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("BAIDU_SERVER_AK="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("missing BAIDU_SERVER_AK in .env")


async def geocode(
    client: httpx.AsyncClient, limiter: RateLimiter, ak: str, address: str
) -> tuple[float, float] | None:
    """把地址解析为百度 BD09 坐标。失败返回 None，由调用方跳过该候选。"""
    for attempt in range(MAX_RETRIES + 1):
        await limiter.acquire()
        try:
            resp = await client.get(
                BASE + "/geocoding/v3/",
                params={"address": address, "output": "json", "ak": ak},
            )
            body = resp.json()
            if body.get("status") == 0:
                loc = body["result"]["location"]
                return float(loc["lat"]), float(loc["lng"])
            if body.get("status") in QUOTA_EXHAUSTED:
                raise QuotaExhausted(f"地理编码返回 {body['status']}：{body.get('message', '')}")
            if body.get("status") in RETRYABLE and attempt < MAX_RETRIES:
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            return None
        except QuotaExhausted:
            raise
        except Exception:
            if attempt < MAX_RETRIES:
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            return None
    return None


def cache_key(keyword: str, lat: float, lng: float) -> Path:
    """按 50 米网格量化坐标后缓存，邻近查询可复用，且配额耗尽后能断点续跑。"""
    qlat, qlng = round(lat * 2000), round(lng * 2000)
    safe = "".join(ch if ch.isalnum() else f"_{ord(ch):x}" for ch in keyword)
    return CACHE_DIR / f"{safe}_{qlat}_{qlng}_{RADIUS_M}.json"


async def count_poi(
    client: httpx.AsyncClient, limiter: RateLimiter, ak: str, budget: Budget,
    keyword: str, lat: float, lng: float,
) -> int | None:
    """返回关键词在 (lat,lng) 周边 RADIUS_M 内的 POI 数量；查询失败返回 None。

    绝不用 0 表示失败——把「查不到」和「真的没有」混为一谈，会让 API 故障
    伪装成服务盲区，产出错误的体检结论。
    """
    path = cache_key(keyword, lat, lng)
    if path.exists():
        budget.cache_hits += 1
        return json.loads(path.read_text(encoding="utf-8"))["total"]

    params = {
        "query": keyword,
        "location": f"{lat:.6f},{lng:.6f}",
        "radius": RADIUS_M,
        "output": "json",
        "page_size": 20,
        "ak": ak,
    }
    for attempt in range(MAX_RETRIES + 1):
        await limiter.acquire()
        budget.api_calls += 1
        try:
            resp = await client.get(BASE + "/place/v2/search", params=params)
            body = resp.json()
            status = body.get("status")
            if status == 0:
                total = int(body.get("total", len(body.get("results", []))))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps({"keyword": keyword, "lat": lat, "lng": lng, "total": total},
                               ensure_ascii=False),
                    encoding="utf-8",
                )
                return total
            if status in QUOTA_EXHAUSTED:
                raise QuotaExhausted(f"地点检索返回 {status}：{body.get('message', '')}")
            if status in RETRYABLE and attempt < MAX_RETRIES:
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            return None
        except QuotaExhausted:
            raise
        except Exception:
            if attempt < MAX_RETRIES:
                await asyncio.sleep(0.5 * (2**attempt))
                continue
            return None
    return None


async def survey_point(
    client, limiter, ak: str, budget: Budget, lat: float, lng: float
) -> dict[str, int | None]:
    """统计一个点位周边各品类的设施数量。

    某品类下只要有一个关键词查询失败，该品类即记为 None（数据不可用），
    不参与后续评分，也不会被误读成盲区。
    """
    result: dict[str, int | None] = {}
    for category, keywords in CATEGORIES.items():
        counts = await asyncio.gather(
            *(count_poi(client, limiter, ak, budget, kw, lat, lng) for kw in keywords)
        )
        result[category] = None if any(c is None for c in counts) else sum(counts)
    return result


def stage1_score(center: dict[str, int | None]) -> tuple[float, int, int]:
    """阶段一粗筛评分：偏好"有设施但不齐全"的候选。

    全类齐全说明配套太好，跑不出盲区；全类为空说明是荒地，测不出分辨力。
    缺 1~3 类是最理想的区间，正好能同时展示覆盖与盲区。
    返回 (评分, 覆盖品类数, 缺失品类数)。
    """
    known = [v for v in center.values() if v is not None]
    covered = sum(1 for v in known if v > 0)
    missing = sum(1 for v in known if v == 0)
    if not known or covered == 0:
        return 0.0, covered, missing
    # 缺失数落在 1~3 时给满分，越偏离越低
    sweet = max(0.0, 3.0 - abs(missing - 2))
    return round(covered * 0.5 + sweet * 2.0, 2), covered, missing


def contrast_score(
    center: dict[str, int | None], probes: list[dict[str, int | None]]
) -> tuple[float, int, int]:
    """阶段二对比度评分：内部落差越大越适合做演示。

    None 表示该品类数据缺失，既不算覆盖也不算缺失，直接跳过。
    返回 (评分, 中心覆盖品类数, 探点中出现的最大缺失品类数)。
    """
    covered = sum(1 for v in center.values() if v is not None and v > 0)
    missing_per_probe = [sum(1 for v in p.values() if v == 0) for p in probes]
    max_missing = max(missing_per_probe) if missing_per_probe else 0
    spread = statistics.pstdev(missing_per_probe) if len(missing_per_probe) > 1 else 0.0
    score = covered * 1.0 + max_missing * 1.5 + spread * 2.0
    return round(score, 2), covered, max_missing


def fmt_counts(row: dict[str, int | None]) -> str:
    return "  ".join(f"{k} {'N/A' if v is None else v}" for k, v in row.items())


async def main() -> None:
    parser = argparse.ArgumentParser(description="用真实 POI 数据挑选样例社区")
    parser.add_argument("--finalists", type=int, default=4, help="进入阶段二的候选数，默认 4")
    args = parser.parse_args()

    ak = load_ak()
    limiter = RateLimiter(MAX_QPS)
    budget = Budget()
    limits = httpx.Limits(max_connections=16, max_keepalive_connections=16, keepalive_expiry=120.0)

    est = len(CANDIDATES) * CALLS_PER_POINT + args.finalists * 4 * CALLS_PER_POINT
    print(f"预算估算：阶段一 {len(CANDIDATES) * CALLS_PER_POINT} 次 + "
          f"阶段二 {args.finalists * 4 * CALLS_PER_POINT} 次 = 约 {est} 次地点检索\n", flush=True)

    aborted: str | None = None
    async with httpx.AsyncClient(timeout=30.0, limits=limits) as client:
        # ---------- 阶段一：中心点粗筛 ----------
        print("=" * 64)
        print("阶段一：中心点粗筛（筛出「有设施但不齐全」的候选）")
        print("=" * 64, flush=True)

        screened: list[tuple[float, Candidate, int, int]] = []
        try:
            for cand in CANDIDATES:
                coords = await geocode(client, limiter, ak, CITY_PREFIX + cand.name)
                if coords is None:
                    print(f"\n{cand.name}\n  [跳过] 地理编码失败", flush=True)
                    continue
                cand.lat, cand.lng = coords
                cand.center = await survey_point(client, limiter, ak, budget, cand.lat, cand.lng)
                score, covered, missing = stage1_score(cand.center)
                print(f"\n{cand.name}  ({cand.note})", flush=True)
                print(f"  {fmt_counts(cand.center)}", flush=True)
                print(f"  粗筛评分 {score}（覆盖 {covered} 类，缺失 {missing} 类）", flush=True)
                screened.append((score, cand, covered, missing))
        except QuotaExhausted as exc:
            aborted = f"阶段一中断：{exc}"
            print(f"\n[中断] {exc}", flush=True)

        if not screened:
            raise SystemExit(aborted or "阶段一未取得任何有效数据")

        # ---------- 阶段二：决赛候选四向细测 ----------
        screened.sort(key=lambda r: r[0], reverse=True)
        finalists = [c for _s, c, _cv, _m in screened[: args.finalists]]

        print("\n" + "=" * 64)
        print(f"阶段二：四向探点细测（仅对粗筛前 {len(finalists)} 名）")
        print("=" * 64, flush=True)

        detailed: list[tuple[float, Candidate, int, int, list[int]]] = []
        try:
            for cand in finalists:
                print(f"\n{cand.name}", flush=True)
                for dlat, dlng in probe_offsets(cand.lat):
                    cand.probes.append(
                        await survey_point(
                            client, limiter, ak, budget, cand.lat + dlat, cand.lng + dlng
                        )
                    )
                missing = [sum(1 for v in p.values() if v == 0) for p in cand.probes]
                score, covered, max_missing = contrast_score(cand.center, cand.probes)
                print(f"  四向探点缺失品类数：{missing}", flush=True)
                print(f"  对比度评分 {score}（中心覆盖 {covered} 类，探点最大缺失 {max_missing} 类）",
                      flush=True)
                detailed.append((score, cand, covered, max_missing, missing))
        except QuotaExhausted as exc:
            aborted = f"阶段二中断：{exc}"
            print(f"\n[中断] {exc}", flush=True)

    print(f"\n{budget.summary()}", flush=True)

    if not detailed:
        raise SystemExit(aborted or "阶段二未取得任何有效数据")

    detailed.sort(key=lambda r: r[0], reverse=True)
    best = detailed[0]
    worst_ref = screened[-1][1]  # 粗筛最低分者作为对照样例

    lines = [
        "# 样例社区选址报告",
        "",
        f"- 目标城区：{CITY_PREFIX}",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 判定半径：{RADIUS_M} 米",
        f"- 候选数量：{len(CANDIDATES)}（该区全部街道与镇）",
        f"- 配额消耗：{budget.summary()}",
        "",
        "## 选址标准",
        "",
        "样例社区不是设施越多越好。演示要同时展示「覆盖统计」与「盲区识别」两项能力，",
        "因此需要一个内部落差明显的社区：既有配套完善的区块，也有确实缺设施的角落。",
        "全覆盖的成熟城区跑不出盲区，纯荒地又显不出算法分辨力。",
        "",
        "为节省地点检索的日配额，采用两阶段渐进筛选：",
        "",
        "1. **阶段一**：仅测各候选中心点，偏好「有设施但缺 1~3 类」的候选；",
        f"2. **阶段二**：只对粗筛前 {args.finalists} 名做四向探点，测内部均衡度。",
        "",
        "一次性全量精细测量需约 500 次检索，两阶段约 180 次。",
        "",
        "## 阶段一：中心点粗筛",
        "",
        "| 排名 | 街道/镇 | 粗筛评分 | 覆盖品类 | 缺失品类 | " + " | ".join(CATEGORIES) + " |",
        "| --- | --- | --- | --- | --- | " + " | ".join("---" for _ in CATEGORIES) + " |",
    ]
    for i, (score, cand, covered, missing) in enumerate(screened, 1):
        counts = " | ".join(
            "N/A" if cand.center.get(k) is None else str(cand.center[k]) for k in CATEGORIES
        )
        lines.append(f"| {i} | {cand.name} | {score} | {covered} | {missing} | {counts} |")

    lines += [
        "",
        "## 阶段二：内部均衡度细测",
        "",
        "| 排名 | 街道/镇 | 对比度评分 | 中心覆盖品类 | 探点最大缺失 | 四向探点缺失分布 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for i, (score, cand, covered, max_missing, missing) in enumerate(detailed, 1):
        lines.append(
            f"| {i} | {cand.name} | {score} | {covered} | {max_missing} | {missing} |"
        )

    lines += [
        "",
        "## 结论",
        "",
        f"**主样例：{CITY_PREFIX}{best[1].name}**",
        "",
        f"- 坐标（BD09）：{best[1].lat:.6f}, {best[1].lng:.6f}",
        f"- 形态：{best[1].note}",
        f"- 对比度评分 {best[0]}，中心点覆盖 {best[2]} 类设施，"
        f"四向探点中最多有 {best[3]} 类完全缺失",
        "",
        "内部落差明显，能同时展示覆盖统计与盲区识别两项能力。",
        "",
        f"**对照样例：{CITY_PREFIX}{worst_ref.name}**",
        "",
        f"- 坐标（BD09）：{worst_ref.lat:.6f}, {worst_ref.lng:.6f}",
        f"- 形态：{worst_ref.note}",
        "",
        "作为反面对照写进测试报告，用于验证系统在配套形态迥异的社区上都能给出合理结论。",
        "",
        "> 中心点坐标在正式应用中可由用户自由拖拽修改，此处仅为默认演示值。",
    ]
    if aborted:
        lines += ["", f"> 探测未跑完：{aborted}", "> 排名仅基于已完成的候选，配额恢复后重跑可补齐。"]

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n主样例：{CITY_PREFIX}{best[1].name}（对比度评分 {best[0]}）")
    print(f"报告已写入 {REPORT_PATH}")


if __name__ == "__main__":
    try:
        with single_instance("pick_sample_area"):
            asyncio.run(main())
    except AlreadyRunning as exc:
        raise SystemExit(str(exc))
