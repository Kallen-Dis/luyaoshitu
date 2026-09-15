"""单接口精细探测：区分 301/302（配额耗尽）与 401/402（瞬时并发超限）。

配额探测中出现两个异常需要定性：
  - 地点检索在并发 2 就报配额类错误 —— 是日配额被打光，还是瞬时策略？
  - 批量算路在并发 1 就报限流 —— 是真实上限极低，还是被预热请求的余波拖累？
两者的区别决定架构：前者必须靠缓存与离线快照绕开，后者只需串行化加退避。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
BASE = "https://api.map.baidu.com"
ANCHOR = (39.983424, 116.322987)

CODE = {
    0: "成功",
    301: "永久配额超限（额度用尽，已停用）",
    302: "天配额超限（当日额度用尽）",
    401: "瞬时并发超限（可退避重试）",
    402: "瞬时并发超限-黑名单",
    2: "请求参数非法",
}


def load_ak() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("BAIDU_SERVER_AK="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("missing BAIDU_SERVER_AK")


def desc(status) -> str:
    return CODE.get(status, f"其他({status})")


async def probe(client, label: str, path: str, params: dict) -> int | None:
    start = time.perf_counter()
    try:
        resp = await client.get(BASE + path, params=params)
        body = resp.json()
        status = body.get("status")
        extra = ""
        if body.get("message") and status != 0:
            extra = f'  message="{body["message"]}"'
        print(
            f"  {label:<26} status={status:<5} {desc(status):<24} "
            f"{time.perf_counter() - start:.2f}s{extra}"
        )
        return status
    except Exception as exc:
        print(f"  {label:<26} 异常 {type(exc).__name__}")
        return None


async def main() -> None:
    ak = load_ak()
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=20, keepalive_expiry=120.0)

    async with httpx.AsyncClient(timeout=30.0, limits=limits) as client:
        print("\n【一】完全冷却后的单发请求（间隔 3 秒，确认额度是否真的耗尽）")
        for i in range(4):
            await probe(
                client,
                f"地点检索 #{i+1}",
                "/place/v2/search",
                {
                    "query": "药店",
                    "location": f"{ANCHOR[0]+i*0.001:.6f},{ANCHOR[1]:.6f}",
                    "radius": 1000,
                    "output": "json",
                    "ak": ak,
                },
            )
            await asyncio.sleep(3.0)

        print()
        for i in range(4):
            await probe(
                client,
                f"批量算路 #{i+1}",
                "/routematrix/v2/walking",
                {
                    "origins": f"{ANCHOR[0]},{ANCHOR[1]}",
                    "destinations": f"{ANCHOR[0]+0.01+i*0.001:.6f},{ANCHOR[1]+0.01:.6f}",
                    "output": "json",
                    "ak": ak,
                },
            )
            await asyncio.sleep(3.0)

        print("\n【二】串行连发（无间隔，测每秒可稳定发出的次数）")
        for label, path, mk in (
            ("地点检索", "/place/v2/search", lambda i: {
                "query": "药店", "location": f"{ANCHOR[0]+i*0.0009:.6f},{ANCHOR[1]:.6f}",
                "radius": 1000, "output": "json", "ak": ak}),
            ("批量算路", "/routematrix/v2/walking", lambda i: {
                "origins": f"{ANCHOR[0]},{ANCHOR[1]}",
                "destinations": f"{ANCHOR[0]+0.02+i*0.001:.6f},{ANCHOR[1]+0.01:.6f}",
                "output": "json", "ak": ak}),
        ):
            start = time.perf_counter()
            codes = []
            for i in range(8):
                codes.append(await probe(client, f"{label} 连发 #{i+1}", path, mk(i)))
            ok = sum(1 for c in codes if c == 0)
            span = time.perf_counter() - start
            print(f"  -> {label}：{ok}/8 成功，耗时 {span:.1f}s，实测约 {8/span:.1f} 次/秒\n")


if __name__ == "__main__":
    asyncio.run(main())
