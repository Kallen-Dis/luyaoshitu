"""百度地图 Web 服务 API 配额与限流探测脚本。

用途：在正式开发前摸清服务端 AK 的真实能力边界，为后续限流器与批量调度提供参数依据。
探测三件事：
  1. 连通性 —— 各接口是否已开通、返回是否正常；
  2. 批量算路单次终点数上限 —— 决定分批粒度；
  3. 并发上限（QPS）—— 逐级加压直到触发限流，定位阈值。

结果写入 reports/quota-report.md，同时在控制台打印摘要。

用法：
    python scripts/probe_quota.py                 # 完整探测
    python scripts/probe_quota.py --skip-qps      # 仅连通性与批量上限，省配额
    python scripts/probe_quota.py --max-conc 40   # 调整并发加压上限
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
from _singleton import AlreadyRunning, single_instance

ROOT = Path(__file__).resolve().parent.parent
REPORT_PATH = ROOT / "reports" / "quota-report.md"

BASE_URL = "https://api.map.baidu.com"

# 探测基准点：北京中关村。仅用于发起真实请求，不影响结论。
ANCHOR_LAT, ANCHOR_LNG = 39.983424, 116.322987

# 百度地图 Web 服务 API 状态码含义，用于把裸数字翻译成可读结论。
STATUS_MEANING = {
    -1: "参数名错误或服务端未识别请求",
    0: "成功",
    1: "服务器内部错误",
    2: "请求参数非法",
    3: "权限校验失败",
    4: "配额校验失败",
    5: "AK 不存在或非法",
    101: "服务未开启",
    102: "不通过白名单或签名错误",
    200: "APP 不存在，AK 有误或已删除",
    210: "APP IP 校验失败",
    211: "APP SN 校验失败",
    220: "APP Referer 校验失败",
    240: "APP 服务被禁用",
    251: "APP 用户被禁用",
    260: "服务不存在",
    261: "服务被禁用",
    301: "永久配额超限，已停用",
    302: "天配额超限，已关闭",
    401: "当前并发量已超过约定并发配额",
    402: "当前并发量已超过约定并发配额（黑名单）",
}

# 并发加压梯度。命中限流即停止，避免无谓消耗配额。
CONCURRENCY_LADDER = [1, 2, 3, 5, 8, 12, 16, 20, 25, 30, 40, 50]

# 链路诊断（scripts/probe_network.py）结论：新建连接需 2.5s 且会随机 ConnectError，
# 复用连接仅 0.54s。故全程共用一个带 keepalive 的连接池，并放宽超时，
# 避免把「TLS 握手风暴」误判成「服务端限流」。
HTTP_TIMEOUT = 30.0
HTTP_LIMITS = httpx.Limits(max_connections=60, max_keepalive_connections=60, keepalive_expiry=120.0)


def load_env(path: Path) -> dict[str, str]:
    """读取 .env 文件。刻意不引入 python-dotenv，保持探测脚本零额外依赖。"""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def describe(status: int | None) -> str:
    if status is None:
        return "无响应"
    return STATUS_MEANING.get(status, f"未知状态码 {status}")


@dataclass
class Probe:
    """一个待探测的接口。build 按序号生成互不相同的参数，避免命中服务端缓存。"""

    name: str
    path: str
    build: callable
    qps_test: bool = True


@dataclass
class BurstResult:
    concurrency: int
    ok: int = 0
    throttled: int = 0
    quota_exceeded: int = 0
    other: dict[int, int] = field(default_factory=dict)
    errors: int = 0
    latencies: list[float] = field(default_factory=list)

    @property
    def p50(self) -> float:
        return statistics.median(self.latencies) if self.latencies else 0.0

    @property
    def hit_limit(self) -> bool:
        return self.throttled > 0 or self.quota_exceeded > 0


def build_probes(ak: str) -> list[Probe]:
    def geocoding(i: int) -> dict:
        return {"address": f"北京市海淀区中关村南大街{i % 50 + 1}号", "output": "json", "ak": ak}

    def place_search(i: int) -> dict:
        return {
            "query": "药店",
            "location": f"{ANCHOR_LAT + i * 0.0007:.6f},{ANCHOR_LNG:.6f}",
            "radius": 1000,
            "output": "json",
            "page_size": 5,
            "ak": ak,
        }

    def matrix_walking(i: int) -> dict:
        dests = "|".join(
            f"{ANCHOR_LAT + (i + k) * 0.0006:.6f},{ANCHOR_LNG + k * 0.0006:.6f}" for k in range(2)
        )
        return {
            "origins": f"{ANCHOR_LAT:.6f},{ANCHOR_LNG:.6f}",
            "destinations": dests,
            "output": "json",
            "ak": ak,
        }

    def direction_walking(i: int) -> dict:
        return {
            "origin": f"{ANCHOR_LAT:.6f},{ANCHOR_LNG:.6f}",
            "destination": f"{ANCHOR_LAT + (i + 1) * 0.0008:.6f},{ANCHOR_LNG + 0.004:.6f}",
            "ak": ak,
        }

    def district_search(i: int) -> dict:
        # 该接口用 keyword 而非 query 传关键词，用 query 会返回 status=-1 "keyword is empty"
        return {"keyword": "海淀区", "sub_admin": 0, "extensions_code": 1, "ak": ak}

    return [
        Probe("地理编码", "/geocoding/v3/", geocoding),
        Probe("地点检索（周边）", "/place/v2/search", place_search),
        Probe("批量算路（步行）", "/routematrix/v2/walking", matrix_walking),
        Probe("步行路线规划", "/directionlite/v1/walking", direction_walking),
        Probe("行政区划检索", "/api_region_search/v1/", district_search, qps_test=False),
    ]


async def fetch(client: httpx.AsyncClient, probe: Probe, index: int) -> tuple[int | None, float]:
    """发起一次请求，返回 (业务状态码, 耗时秒)。网络层异常以 None 表示。"""
    start = time.perf_counter()
    try:
        resp = await client.get(BASE_URL + probe.path, params=probe.build(index))
        elapsed = time.perf_counter() - start
        try:
            return int(resp.json().get("status", -1)), elapsed
        except Exception:
            return -1, elapsed
    except Exception:
        return None, time.perf_counter() - start


async def check_connectivity(client: httpx.AsyncClient, probes: list[Probe]) -> list[dict]:
    results = []
    for probe in probes:
        status, elapsed = await fetch(client, probe, 0)
        results.append(
            {
                "name": probe.name,
                "path": probe.path,
                "status": status,
                "meaning": describe(status),
                "ok": status == 0,
                "latency": elapsed,
            }
        )
        flag = "OK " if status == 0 else "FAIL"
        print(f"  [{flag}] {probe.path} -> status={status} ({elapsed:.2f}s)")
        await asyncio.sleep(0.4)
    return results


async def probe_matrix_limit(client: httpx.AsyncClient, ak: str, ceiling: int = 120) -> dict:
    """二分查找批量算路单次请求可接受的最大终点数。

    ceiling 必须是一个**已知不可行**的上界。配额调整后上限可能被放宽，
    沿用旧的 120 会让二分收敛到 119 而看不出真实上限，故做成参数。
    """

    async def try_count(n: int) -> int | None:
        dests = "|".join(
            f"{ANCHOR_LAT + k * 0.0004:.6f},{ANCHOR_LNG + k * 0.0004:.6f}" for k in range(n)
        )
        params = {
            "origins": f"{ANCHOR_LAT:.6f},{ANCHOR_LNG:.6f}",
            "destinations": dests,
            "output": "json",
            "ak": ak,
        }
        try:
            resp = await client.get(BASE_URL + "/routematrix/v2/walking", params=params)
            return int(resp.json().get("status", -1))
        except Exception:
            return None

    low, high = 1, max(2, ceiling)  # low 已知可行，high 已知不可行
    tested: list[tuple[int, int | None]] = []
    while low + 1 < high:
        mid = (low + high) // 2
        status = await try_count(mid)
        tested.append((mid, status))
        print(f"  {mid:>3} 个终点 -> status={status} ({describe(status)})")
        if status == 0:
            low = mid
        else:
            high = mid
        await asyncio.sleep(0.6)
    # low == ceiling - 1 说明加压到上界仍然成功，真实上限可能更高
    return {"max_destinations": low, "tested": tested, "saturated": low >= ceiling - 1}


async def warm_pool(client: httpx.AsyncClient, probe: Probe, size: int) -> None:
    """预热连接池：先并发建立好 size 条长连接，使后续加压只测服务端限流，
    不混入 TLS 握手开销。预热结果一律丢弃。"""
    await asyncio.gather(*(fetch(client, probe, 900 + i) for i in range(size)))
    await asyncio.sleep(2.0)


async def probe_qps(client: httpx.AsyncClient, probe: Probe, max_conc: int) -> list[BurstResult]:
    """逐级并发加压，直到触发限流或达到上限。"""
    results: list[BurstResult] = []
    for level in CONCURRENCY_LADDER:
        if level > max_conc:
            break
        outcomes = await asyncio.gather(*(fetch(client, probe, i) for i in range(level)))
        burst = BurstResult(concurrency=level)
        for status, elapsed in outcomes:
            burst.latencies.append(elapsed)
            if status is None:
                burst.errors += 1
            elif status == 0:
                burst.ok += 1
            elif status in (401, 402):
                burst.throttled += 1
            elif status in (301, 302):
                burst.quota_exceeded += 1
            else:
                burst.other[status] = burst.other.get(status, 0) + 1
        results.append(burst)
        flag = "限流" if burst.throttled else ("配额超限" if burst.quota_exceeded else "正常")
        print(
            f"  并发 {level:>2}: 成功 {burst.ok:>2}/{level}"
            f"  限流 {burst.throttled}  配额 {burst.quota_exceeded}"
            f"  p50 {burst.p50:.2f}s  [{flag}]"
        )
        if burst.hit_limit:
            break
        await asyncio.sleep(2.0)  # 让限流窗口回落，避免上一级余波干扰下一级
    return results


def safe_qps(bursts: list[BurstResult]) -> tuple[int, str]:
    """由加压结果推导建议的安全 QPS：取最后一个全成功档位的 80%。"""
    clean = [b.concurrency for b in bursts if not b.hit_limit and b.ok == b.concurrency]
    if not clean:
        return 1, "所有档位均未全部成功，建议从 1 起步并人工复核"
    ceiling = max(clean)
    hit = next((b.concurrency for b in bursts if b.hit_limit), None)
    if hit is None:
        return ceiling, f"加压至 {ceiling} 仍未触发限流，上限可能更高（受本次加压上限所限）"
    return max(1, int(ceiling * 0.8)), f"{hit} 并发触发限流，{ceiling} 并发安全，取 80% 余量"


def render_report(conn, matrix, qps_results, elapsed, total_requests) -> str:
    lines = [
        "# 百度地图 API 配额与限流探测报告",
        "",
        f"- 探测时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 总耗时：{elapsed:.1f} 秒",
        f"- 消耗请求数：约 {total_requests} 次",
        "",
        "## 一、接口连通性",
        "",
        "| 接口 | 路径 | 状态码 | 含义 | 延迟 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in conn:
        lines.append(
            f"| {r['name']} | `{r['path']}` | {r['status']} "
            f"| {r['meaning']} | {r['latency']:.2f}s |"
        )

    lines += [
        "",
        "## 二、批量算路单次终点数上限",
        "",
        f"**最大终点数：{matrix['max_destinations']}**（1 个起点对 N 个终点）",
        "",
        "该数值直接决定等时圈采样的分批粒度：采样点总数除以它，即为每次体检需要发出的批量算路请求数。",
        "",
        "| 测试终点数 | 状态码 | 含义 |",
        "| --- | --- | --- |",
    ]
    for n, status in matrix["tested"]:
        lines.append(f"| {n} | {status} | {describe(status)} |")

    lines += ["", "## 三、并发上限探测", ""]
    if not qps_results:
        lines.append("本次运行跳过了并发探测（`--skip-qps`）。")
    for name, bursts in qps_results:
        rec, reason = safe_qps(bursts)
        lines += [
            f"### {name}",
            "",
            f"**建议安全 QPS：{rec}** —— {reason}",
            "",
            "| 并发数 | 成功 | 限流(401/402) | 配额超限(301/302) | 网络异常 | p50 延迟 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for b in bursts:
            lines.append(
                f"| {b.concurrency} | {b.ok} | {b.throttled} | {b.quota_exceeded} "
                f"| {b.errors} | {b.p50:.2f}s |"
            )
        lines.append("")

    lines += [
        "## 四、结论与限流参数建议",
        "",
        "将下列参数写入 `.env`，作为后端令牌桶限流器与批量调度器的初始配置：",
        "",
        "```env",
        f"BAIDU_MATRIX_BATCH_SIZE={matrix['max_destinations']}",
    ]
    if qps_results:
        overall = min(safe_qps(b)[0] for _, b in qps_results)
        lines.append(f"BAIDU_MAX_QPS={overall}")
    lines += [
        "```",
        "",
        "> 注：并发上限受账号等级、当日剩余配额与服务端动态策略影响，探测值为当次快照。",
        "> 生产配置应在此基础上再留余量，并依赖运行时的退避重试与降级兜底应对波动。",
    ]
    return "\n".join(lines) + "\n"


async def main() -> None:
    parser = argparse.ArgumentParser(description="百度地图 API 配额与限流探测")
    parser.add_argument(
        "--skip-qps", action="store_true", help="跳过并发加压，仅做连通性与批量上限探测"
    )
    parser.add_argument("--max-conc", type=int, default=30, help="并发加压的最高档位，默认 30")
    parser.add_argument(
        "--matrix-ceiling",
        type=int,
        default=120,
        help="批量算路终点数二分的上界（须已知不可行）。配额放宽后可调高，如 600",
    )
    args = parser.parse_args()

    env = load_env(ROOT / ".env")
    ak = env.get("BAIDU_SERVER_AK", "")
    if not ak or ak == "your_server_ak_here":
        raise SystemExit("未在 .env 中找到有效的 BAIDU_SERVER_AK，请参照 .env.example 配置后重试。")

    probes = build_probes(ak)
    started = time.perf_counter()
    counter = 0

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, limits=HTTP_LIMITS) as client:
        print("\n[1/3] connectivity")
        conn = await check_connectivity(client, probes)
        counter += len(probes)

        print("\n[2/3] routematrix destination limit")
        matrix = await probe_matrix_limit(client, ak, args.matrix_ceiling)
        counter += len(matrix["tested"])

        qps_results: list[tuple[str, list[BurstResult]]] = []
        if args.skip_qps:
            print("\n[3/3] qps probe skipped")
        else:
            print("\n[3/3] qps ladder")
            for probe in probes:
                if not probe.qps_test or not any(c["path"] == probe.path and c["ok"] for c in conn):
                    continue
                print(f" {probe.path}")
                await warm_pool(client, probe, min(args.max_conc, 12))
                counter += min(args.max_conc, 12)
                bursts = await probe_qps(client, probe, args.max_conc)
                counter += sum(b.concurrency for b in bursts)
                qps_results.append((probe.name, bursts))
                # 换接口前充分冷却。实测 3 秒不够：预热用的 12 并发余波会让下一个
                # 接口在并发 1~2 就报 401，把瞬时余波误读成极低的并发上限。
                await asyncio.sleep(15.0)

    elapsed = time.perf_counter() - started
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        render_report(conn, matrix, qps_results, elapsed, counter), encoding="utf-8"
    )
    print(f"\nDone in {elapsed:.1f}s, ~{counter} requests. Report -> {REPORT_PATH}")


if __name__ == "__main__":
    try:
        with single_instance("probe_quota"):
            asyncio.run(main())
    except AlreadyRunning as exc:
        raise SystemExit(str(exc)) from exc
