"""只读缓存重建 v3 样例。--publish 发布通过的校正或附带拒绝原因的原圈；禁止网络。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.baidu.client import BaiduMapClient, request_observer  # noqa: E402
from app.baidu.errors import BaiduApiError  # noqa: E402
from app.isochrone.algorithm import IsochroneConfig, compute_isochrone  # noqa: E402
from app.isochrone.budget import FusionBudgetClient  # noqa: E402
from app.isochrone.fusion import apply_fusion, sync_heat_measurements  # noqa: E402
from app.isochrone.geometry import polygon_of  # noqa: E402
from app.isochrone.gridcheck import GridcheckConfig, cross_check, ray_samples_of  # noqa: E402
from app.isochrone.refine import RefineOptions  # noqa: E402
from app.poi.catalog import CATEGORIES  # noqa: E402
from app.poi.collect import CategoryResult, CleanStats, CoverageResult, Poi  # noqa: E402
from app.report.blindspot import BlindspotConfig, identify_blindspots  # noqa: E402
from app.report.score import build_report  # noqa: E402


def restore_coverage(data, center):
    """保持原设施集合与校门，不重新检索或改变买菜判定。"""
    results = []
    for category in CATEGORIES:
        if category.name in data.get("failed_categories", []):
            continue
        pois = []
        for place in data.get("places", []):
            if place["category"] != category.name:
                continue
            pois.append(
                Poi(
                    name=place["name"],
                    lat=place["lat"],
                    lng=place["lng"],
                    category=category.name,
                    entries=tuple(
                        (e["lat"], e["lng"], e.get("name", "入口"))
                        for e in place.get("entries", [])
                    ),
                    fresh_status=place.get("fresh_status"),
                    fresh_evidence=place.get("fresh_evidence"),
                )
            )
        stats = data.get("clean_stats", {}).get(category.name, {})
        results.append(
            CategoryResult(
                category=category.name,
                pois=pois,
                stats=CleanStats(
                    **{
                        k: stats.get(k, 0)
                        for k in (
                            "raw",
                            "invalid",
                            "excluded",
                            "out_of_range",
                            "duplicated",
                        )
                    }
                ),
                search_metadata=data.get("search_metadata", {}).get(category.name, []),
            )
        )
    return CoverageResult(center, data["radius_m"], results, data.get("failed_categories", []))


async def build(name, publish=False, *, live=False, max_pairs=0, max_routes=0, ledger=None):
    path = ROOT / "data" / "samples" / f"isochrone-{name}-15min.geojson"
    backup = ROOT / ".cache" / "fusion-v3-backups" / path.name
    data = json.loads((backup if backup.exists() else path).read_text(encoding="utf-8"))
    old = json.loads(json.dumps(data["properties"]))
    if old.get("geometry_method") == "fusion":
        raise ValueError("需要未融合的原样例作为基线")
    center = (old["center"]["lat"], old["center"]["lng"])
    if live and ledger is None:
        raise ValueError("真实补测必须指定持久化预算账本，重复运行不能重置额度")
    used = {"pairs": 0, "routes": 0}
    limits = {"pairs": max_pairs, "routes": max_routes}
    if live:
        ledger = Path(ledger).resolve()
        ledger.relative_to(ROOT.resolve() / ".cache")
        ledger.parent.mkdir(parents=True, exist_ok=True)
        if ledger.exists():
            saved = json.loads(ledger.read_text(encoding="utf-8"))
            if saved["sample"] != name or saved["limits"] != limits:
                raise ValueError("账本地点或额度与本次参数不同")
            used.update(saved["used"])

    def guard(endpoint, params):
        if "routematrix" in endpoint:
            kind = "pairs"
            cost = len(params["origins"].split("|")) * len(params["destinations"].split("|"))
        elif "directionlite" in endpoint and "walking" in endpoint:
            kind, cost = "routes", 1
        else:
            raise BaiduApiError(-1, "本次仅授权测距与步行路线，其他请求禁止", endpoint)
        if used[kind] + cost > limits[kind]:
            raise BaiduApiError(-1, "本轮补测预算已用完（重复运行与重试共用）", endpoint)
        used[kind] += cost
        ledger.write_text(
            json.dumps({"sample": name, "limits": limits, "used": used}),
            encoding="utf-8",
        )

    async with BaiduMapClient() as client:

        async def deny_network(*args, **kwargs):
            raise AssertionError("离线生成禁止网络请求")

        if not live:
            client._request = deny_network
        token = request_observer.set(guard if live else request_observer.get())
        try:
            cached = FusionBudgetClient(client, max_pairs if live else 0, max_routes if live else 0)
            iso = await compute_isochrone(
                cached,
                center,
                IsochroneConfig(minutes=old["minutes"], directions=len(old["rays"])),
                RefineOptions(delay=True),
            )
            if iso.failed_points or iso.refine.routes_ok < len(old["rays"]):
                raise ValueError("射线缓存不完整，原样例未修改")
            checked = await cross_check(
                cached,
                center,
                polygon_of(data),
                old["blindspots"],
                iso.refine,
                ray_samples_of(iso),
                old["area_km2"],
                GridcheckConfig(
                    fusion_mode=True,
                    max_pairs=max_pairs if live else 0,
                    max_routes=max_routes if live else 0,
                ),
            )
            if not checked:
                raise ValueError("无校核数据")
            candidate = checked["fusion"]
            accepted = apply_fusion(data, checked)
            props = data["properties"]
            props["gridcheck"] = checked
            if accepted:
                props["gridcheck"] = checked
                checked.pop("fusion", None)
                coverage = restore_coverage(old["coverage"], center)
                blind = await identify_blindspots(
                    cached,
                    center,
                    data["geometry"],
                    coverage,
                    replace(BlindspotConfig(), grid_alignment=iso.polygon),
                    refine=iso.refine,
                )
                props["blindspots"] = blind.as_dict()
                props["blindspots"]["scope_method"] = "fusion"
                sync_heat_measurements(props["blindspots"], props["fusion"])
                props["coverage"] = coverage.as_dict(data["geometry"])
                props["coverage"]["source"] = old["coverage"].get("source", "样例设施集合")
                props["report"] = build_report(props, props["coverage"], props["blindspots"])
            usage = {
                "matrix_pairs": client.matrix_pairs,
                "requests": dict(client.request_counts),
            }
            if not live:
                assert usage["matrix_pairs"] == 0 and sum(usage["requests"].values()) == 0
            props["fusion_api_usage"] = usage
            props["fusion_budget"] = cached.summary()
            props["data_provenance"] = {
                "facilities_sample_at": old.get("generated_at"),
                "fusion_updated_at": datetime.now().astimezone().isoformat(),
                "note": "设施沿用原快照；补测用量见 fusion_api_usage，未重新检索设施。"
                if live
                else "设施沿用原快照；仅复用缓存，无新增 API 请求。",
            }
            # 失败只保存原几何、原统计及拒绝原因，绝不发布被拒绝的新几何。
            directory = ROOT / ".cache" / "fusion-v3-candidates"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"{name}-candidate.json").write_text(
                json.dumps(candidate, ensure_ascii=False), encoding="utf-8"
            )
            checked.pop("fusion", None)
            (directory / path.name).write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            summary = {
                "sample": name,
                "accepted": accepted,
                "area_km2": props["area_km2"],
                "quality": candidate.get("quality"),
                "validation": checked.get("validation"),
                "parts": len(candidate["geometry"]["coordinates"]),
                "holes": sum(len(f) - 1 for f in candidate["geometry"]["coordinates"]),
                "vertices": sum(len(r) for f in candidate["geometry"]["coordinates"] for r in f),
                "added_km2": candidate["added_area_km2"],
                "removed_km2": candidate["omitted_ray_area_km2"],
                "usage": usage,
                "supplement_ledger": used if live else None,
            }
            (directory / f"{name}-audit.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if publish:
                backup.parent.mkdir(parents=True, exist_ok=True)
                if not backup.exists():
                    backup.write_bytes(path.read_bytes())
                path.write_bytes((directory / path.name).read_bytes())
            print(json.dumps(summary, ensure_ascii=False), flush=True)
        finally:
            request_observer.reset(token)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample", choices=["caoyang", "taopu"])
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--live", action="store_true", help="仅明确授权后使用；默认严格离线")
    parser.add_argument("--max-pairs", type=int, default=0)
    parser.add_argument("--max-routes", type=int, default=0)
    parser.add_argument("--ledger", help="本轮 .cache 内持久化账本，同轮重跑必须复用")
    args = parser.parse_args()
    asyncio.run(
        build(
            args.sample,
            args.publish,
            live=args.live,
            max_pairs=args.max_pairs,
            max_routes=args.max_routes,
            ledger=args.ledger,
        )
    )
