"""网络链路诊断：区分「百度侧限流」与「本机网络慢」。

上一轮配额探测中出现大量 20 秒超时，但同一接口单发又能秒回，怀疑是本机到
api.map.baidu.com 的链路不稳，而非服务端限流。本脚本用三组对照实验定位原因：
  A. 串行基线 —— 逐个发请求，测真实延迟分布；
  B. 连接复用对照 —— 同一连接池 vs 每次新建连接，看 TLS 握手是否为瓶颈；
  C. 温和并发 —— 小并发 + 长超时，观察成功率是否回升。
"""

from __future__ import annotations

import asyncio
import statistics
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
URL = "https://api.map.baidu.com/directionlite/v1/walking"
ANCHOR = (39.983424, 116.322987)

TIMEOUT = 60.0


def load_ak() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("BAIDU_SERVER_AK="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("missing BAIDU_SERVER_AK in .env")


def params(ak: str, i: int) -> dict:
    return {
        "origin": f"{ANCHOR[0]},{ANCHOR[1]}",
        "destination": f"{ANCHOR[0] + (i + 1) * 0.0008:.6f},{ANCHOR[1] + 0.004:.6f}",
        "ak": ak,
    }


async def one(client: httpx.AsyncClient, ak: str, i: int) -> tuple[str, float]:
    start = time.perf_counter()
    try:
        resp = await client.get(URL, params=params(ak, i))
        return str(resp.json().get("status")), time.perf_counter() - start
    except httpx.TimeoutException:
        return "timeout", time.perf_counter() - start
    except Exception as exc:
        return type(exc).__name__, time.perf_counter() - start


def summarize(label: str, results: list[tuple[str, float]]) -> None:
    lat = [d for s, d in results if s == "0"]
    ok = len(lat)
    total = len(results)
    failures = [s for s, _ in results if s != "0"]
    line = f"{label:<28} ok {ok}/{total}"
    if lat:
        line += f"  min {min(lat):5.2f}s  p50 {statistics.median(lat):5.2f}s  max {max(lat):5.2f}s"
    if failures:
        line += f"  fail={sorted(set(failures))}"
    print(line)


async def main() -> None:
    ak = load_ak()
    print(f"timeout = {TIMEOUT}s\n")

    # A. 串行基线：连接复用，逐个发。这是网络链路的真实水平。
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        serial = []
        for i in range(8):
            serial.append(await one(client, ak, i))
            await asyncio.sleep(0.3)
        summarize("A 串行(复用连接)", serial)

    # B. 每次新建连接：与 A 的差值即为 DNS + TLS 握手开销。
    fresh = []
    for i in range(5):
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            fresh.append(await one(client, ak, i + 100))
        await asyncio.sleep(0.3)
    summarize("B 串行(每次新建连接)", fresh)

    # C. 温和并发 + 长超时：若成功率回到 100%，说明此前失败是超时而非限流。
    for level in (3, 5, 8, 12):
        limits = httpx.Limits(max_connections=level, max_keepalive_connections=level)
        async with httpx.AsyncClient(timeout=TIMEOUT, limits=limits) as client:
            results = await asyncio.gather(
                *(one(client, ak, i + 200 + level * 20) for i in range(level))
            )
        summarize(f"C 并发 {level:>2} (60s 超时)", list(results))
        await asyncio.sleep(2.0)


if __name__ == "__main__":
    asyncio.run(main())
