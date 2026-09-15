"""探测批量算路对「起点数 × 终点数」形状的约束。

已知单次请求的点对总数上限是 100，但上限是按乘积算还是对起点数另有限制，
文档没有说明。网格盲区判定依赖多起点装箱（M 个网格 × N 个候选设施装进一次请求），
若起点数另有上限，装箱策略就必须改——否则整块请求失败会被下游读成「测距失败」。

实测结论（2026-09-15）：

- **乘积 ≤ 100 是唯一的形状约束**，起点数本身不设上限：100 起点 × 1 终点、
  50 起点 × 2 终点、10 起点 × 10 终点均正常，101 个点对才报 `status: 2`。
  故网格盲区判定可以放心按乘积装箱，把多个网格并进一次请求。
- 零星出现 `401 并发超限`，与形状无明显相关，属瞬时噪声，退避重试即可。
- 排查时务必先看清状态码：本项目曾把 `302 天配额超限`误读成并发限流，
  照着错误的结论调了半天限流参数。两者都表现为"整块请求拿不到结果"，
  但一个该退避重试、另一个必须立即停手。

用法：
    python scripts/probe_matrix_shape.py            # 仅形状探测，约 14 次请求
    python scripts/probe_matrix_shape.py --burst    # 追加并发对比，多约 12 次

注意：本脚本会实际消耗批量算路配额，结论已记录在上，通常无需重跑。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.config import get_settings  # noqa: E402

LAT, LNG = 31.248, 121.417

# 乘积都不超过 100，只改形状：若失败只与起点数相关，即可定位到起点数上限
SHAPES = [
    (1, 100),
    (2, 50),
    (4, 25),
    (5, 20),
    (6, 16),
    (8, 12),
    (10, 10),
    (12, 8),
    (14, 7),
    (16, 6),
    (20, 5),
    (25, 4),
    (50, 2),
    (100, 1),
]


async def probe(client: httpx.AsyncClient, ak: str, m: int, n: int) -> None:
    origins = "|".join(f"{LAT + i * 0.001:.6f},{LNG:.6f}" for i in range(m))
    dests = "|".join(f"{LAT + 0.01:.6f},{LNG + j * 0.001:.6f}" for j in range(n))
    resp = await client.get(
        "https://api.map.baidu.com/routematrix/v2/walking",
        params={"origins": origins, "destinations": dests, "output": "json", "ak": ak},
    )
    body = resp.json()
    status = body.get("status")
    results = body.get("result") or []
    print(
        f"  {m:>3} 起点 x {n:>3} 终点 = {m * n:>4} 点对"
        f"  status={status}  返回 {len(results)} 条  {body.get('message', '')}"
    )


async def burst(client: httpx.AsyncClient, ak: str, m: int, n: int, times: int) -> None:
    """同时发 times 个同样形状的请求，观察 401 比例。

    用于判断并发配额是按「请求数」还是按「点对数」计量：若按请求数，
    点对多寡不应影响 401 比例；若按点对数，大矩阵会显著更容易被限流。
    """
    origins = "|".join(f"{LAT + i * 0.001:.6f},{LNG:.6f}" for i in range(m))
    dests = "|".join(f"{LAT + 0.01:.6f},{LNG + j * 0.001:.6f}" for j in range(n))
    params = {"origins": origins, "destinations": dests, "output": "json", "ak": ak}

    async def one() -> int:
        resp = await client.get(
            "https://api.map.baidu.com/routematrix/v2/walking", params=params
        )
        return int(resp.json().get("status", -1))

    statuses = await asyncio.gather(*(one() for _ in range(times)), return_exceptions=True)
    tally: dict[str, int] = {}
    for s in statuses:
        key = type(s).__name__ if isinstance(s, BaseException) else f"status={s}"
        tally[key] = tally.get(key, 0) + 1
    detail = "  ".join(f"{k}: {v}" for k, v in sorted(tally.items()))
    print(f"  {times} 并发 x（{m}x{n}={m * n} 点对） -> {detail}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="批量算路形状与并发计量探测")
    parser.add_argument(
        "--burst", action="store_true", help="追加并发计量验证（额外消耗约 12 次请求）"
    )
    args = parser.parse_args()

    ak = get_settings().require_server_ak()
    print("批量算路形状探测（乘积均不超过 100）：")
    async with httpx.AsyncClient(timeout=30.0) as client:
        for m, n in SHAPES:
            await probe(client, ak, m, n)
            await asyncio.sleep(0.5)

        if args.burst:
            print("\n并发配额计量方式（同并发数，只改每请求的点对数）：")
            await asyncio.sleep(10.0)  # 让上一轮的限流窗口回落
            await burst(client, ak, 2, 2, 6)
            await asyncio.sleep(10.0)
            await burst(client, ak, 10, 10, 6)


if __name__ == "__main__":
    asyncio.run(main())
