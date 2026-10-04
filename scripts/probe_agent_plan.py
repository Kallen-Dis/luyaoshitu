"""探测 .env 里的凭据能做什么：Agent Plan 的 Token，以及（可选的）另一对普通 AK。

口径完全不同：

- `BAIDU_MAP_AUTH_TOKEN`（sk-ap- 开头）：百度地图 Agent Plan 的 Token，走
  `/agent_plan/v1/*`，用 `Authorization: Bearer` 鉴权，返回 GCJ02 坐标，额度按 Agent Plan 计。
  应用里的「AI 二次核对」用的就是它。
- `AGENT_PLAN_SERVER_AK` / `AGENT_PLAN_BROWSER_AK`：另一组普通的服务端 / 浏览器端 AK，
  和项目主 AK 同一套接口。不在 .env.example 里，想对照两组 AK 的配额时自己加进 .env；没配就跳过。

    python scripts/probe_agent_plan.py            # Token 1 次 + 两个浏览器端 AK 各 2 次地理编码
    python scripts/probe_agent_plan.py --keys     # 另加服务端 AK 的 5 项服务（批量算路 1 个点对）
    python scripts/probe_agent_plan.py --compare  # --keys 的请求再用主 AK 发一遍，对照状态码

--compare 的用处：主 AK 的批量算路当天已经 302、另一个 AK 同一时刻返回 0，就说明两者的配额分开计。
输出里不打印任何 AK 或 Token。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

import httpx
from _singleton import AlreadyRunning, single_instance

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.baidu.errors import STATUS_MEANING  # noqa: E402
from app.config import ENV_PATH, _load_env_file  # noqa: E402

BASE = "https://api.map.baidu.com"
# 曹杨样例中心（BD09），和快照同一处，结果可以拿来对照
CENTER = (31.247979, 121.416775)
NEAR = (31.249779, 121.416775)  # 正北约 200 米
# 前端开发服务器的来源；浏览器端 AK 的 Referer 白名单应包含 localhost/*
DEV_REFERER = "http://localhost:5173/"

PROBES: list[tuple[str, str, dict[str, str]]] = [
    ("地理编码", "/geocoding/v3/", {"address": "上海市普陀区曹杨新村街道", "output": "json"}),
    (
        "逆地理编码",
        "/reverse_geocoding/v3/",
        {"location": f"{CENTER[0]},{CENTER[1]}", "output": "json"},
    ),
    (
        "地点检索（周边）",
        "/place/v2/search",
        {
            "query": "药店",
            "location": f"{CENTER[0]},{CENTER[1]}",
            "radius": "500",
            "page_size": "1",
            "output": "json",
        },
    ),
    (
        "批量算路（步行，1 个点对）",
        "/routematrix/v2/walking",
        {
            "origins": f"{CENTER[0]},{CENTER[1]}",
            "destinations": f"{NEAR[0]},{NEAR[1]}",
            "output": "json",
        },
    ),
    (
        "步行路线规划",
        "/directionlite/v1/walking",
        {"origin": f"{CENTER[0]},{CENTER[1]}", "destination": f"{NEAR[0]},{NEAR[1]}"},
    ),
]

Row = tuple[str, int | None, str, float]


def _value(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return None if not value or value.startswith("your_") else value


def _summary(body: dict) -> str:
    """成功时给一句能核对的结果，不打印原始数据。"""
    result = body.get("result")
    if isinstance(body.get("results"), list):
        first = body["results"][0]["name"] if body["results"] else "无"
        return f"返回 {len(body['results'])} 条，第一条「{first}」"
    if isinstance(result, list) and result and "distance" in result[0]:
        return f"步行 {result[0]['distance'].get('value')} 米"
    if isinstance(result, dict) and "routes" in result:
        routes = result["routes"] or [{}]
        return f"路线 {routes[0].get('distance')} 米"
    if isinstance(result, dict) and "location" in result:
        loc = result["location"]
        return f"坐标 {float(loc.get('lat')):.5f}, {float(loc.get('lng')):.5f}"
    if isinstance(result, dict) and "formatted_address" in result:
        return result["formatted_address"]
    return ""


async def _get(
    client: httpx.AsyncClient, label: str, path: str, params: dict, headers: dict | None = None
) -> Row:
    start = time.perf_counter()
    try:
        resp = await client.get(BASE + path, params=params, headers=headers or {})
        body = resp.json()
        status = int(body.get("status", -1))
        note = _summary(body) if status == 0 else str(body.get("message") or "")
    except (httpx.HTTPError, ValueError) as exc:
        status, note = None, type(exc).__name__
    await asyncio.sleep(0.5)  # 逐个慢慢发，不碰并发上限
    return label, status, note[:60], time.perf_counter() - start


async def _keys(client: httpx.AsyncClient, ak: str) -> list[Row]:
    return [await _get(client, label, path, {**p, "ak": ak}) for label, path, p in PROBES]


async def _browser(client: httpx.AsyncClient, ak: str) -> list[Row]:
    """浏览器端 AK 拿地理编码试一下：只能看出这个应用开没开 Web 服务。

    实测（2026-10-04）两个浏览器端 AK 都返回 240（没开地理编码服务），这是浏览器端应用的常态，
    说明不了 JS API 能不能用：JS API 的鉴权在浏览器里完成，只能把它换进 .env 打开页面看。
    """
    _, path, params = PROBES[0]
    with_ref = await _get(
        client, "带 Referer（localhost）", path, {**params, "ak": ak}, {"Referer": DEV_REFERER}
    )
    without = await _get(client, "不带 Referer", path, {**params, "ak": ak})
    return [with_ref, without]


async def _token(client: httpx.AsyncClient, token: str) -> list[Row]:
    """Agent Plan：地理编码最便宜，1 次就能确认 Token 有效。"""
    start = time.perf_counter()
    try:
        resp = await client.get(
            BASE + "/agent_plan/v1/geocoding",
            params={"address": "上海市普陀区曹杨新村街道", "region": "上海市"},
            headers={"Authorization": f"Bearer {token}"},
        )
        body = resp.json()
        status = int(body.get("status", -1)) if "status" in body else resp.status_code
        result = body.get("result") or {}
        loc = result.get("location") if isinstance(result, dict) else None
        if status == 0 and loc:
            note = f"GCJ02 坐标 {float(loc['lat']):.5f}, {float(loc['lng']):.5f}"
        else:
            note = str(body.get("message") or body.get("msg") or "")[:60]
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        status, note = None, type(exc).__name__
    return [("Agent Plan 地理编码", status, note, time.perf_counter() - start)]


def _print(title: str, rows: list[Row]) -> None:
    print(f"\n【{title}】")
    for label, status, note, secs in rows:
        meaning = "网络异常" if status is None else STATUS_MEANING.get(status, f"其他（{status}）")
        print(f"  {label:<20} status={status!s:<5} {meaning:<16} {secs:.2f}s  {note}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="探测 Agent Plan Token 与第二组 AK")
    parser.add_argument("--keys", action="store_true", help="测服务端 AK 的 5 项服务")
    parser.add_argument("--compare", action="store_true", help="--keys 的请求再用主 AK 发一遍")
    args = parser.parse_args()

    _load_env_file(ENV_PATH)
    async with httpx.AsyncClient(timeout=20.0) as client:
        token = _value("BAIDU_MAP_AUTH_TOKEN")
        if token:
            _print("Agent Plan Token（BAIDU_MAP_AUTH_TOKEN）", await _token(client, token))
        else:
            print("\n没有配置 BAIDU_MAP_AUTH_TOKEN，跳过 Agent Plan。")

        for name in ("AGENT_PLAN_BROWSER_AK", "VITE_BAIDU_BROWSER_AK"):
            ak = _value(name)
            if ak:
                _print(f"浏览器端 AK（{name}）", await _browser(client, ak))

        if args.keys or args.compare:
            ak = _value("AGENT_PLAN_SERVER_AK")
            if ak:
                _print("服务端 AK（AGENT_PLAN_SERVER_AK）", await _keys(client, ak))
            main_ak = _value("BAIDU_SERVER_AK") if args.compare else None
            if main_ak:
                _print("项目主 AK（BAIDU_SERVER_AK，对照）", await _keys(client, main_ak))

    print(
        "\n读法：0 = 可用；220 = Referer 不在白名单（浏览器端 AK 不带 Referer 时就该是它）；"
        "240 / 260 / 261 = 没开通或禁用了该服务；210 = IP 白名单不对；302 = 当日配额已用完。"
    )


if __name__ == "__main__":
    try:
        with single_instance("probe_agent_plan"):
            asyncio.run(main())
    except AlreadyRunning as exc:
        raise SystemExit(str(exc)) from exc
