"""示例数据仓库。

快照里是一次完整体检的全部派生结果：等时圈多边形、各方向半径、圈内品类计数、
网格盲区点位。应用默认从快照读取，实时计算仅作为可选路径。

地点检索的日配额已提升至 3000 次，快照不再是"配额不够"的被动妥协，
但仍然必要：它让评审方零 API 消耗即可跑通演示，也让首屏不受网络与配额波动影响，
正对应交付要求中"配置好示例数据，保证评审方能快速跑通演示"。

快照带设施名称和坐标，打开样例就能在地图上打点；不含电话和街道地址。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import PROJECT_ROOT
from .report.score import build_report
from .report.wide_grid import polygon_of

SAMPLES_DIR = PROJECT_ROOT / "data" / "samples"


@dataclass(frozen=True)
class SampleMeta:
    id: str
    name: str
    minutes: float
    center: dict[str, float]
    area_km2: float
    mean_radius_m: float
    compactness: float
    generated_at: str | None
    # 列表接口就要带对比数字：评审切样例前应已看见「面积只剩几成、圈内有没有设施」
    grade: str | None
    total: float | None
    area_ratio: float | None
    facilities_in: int | None
    facilities_nearby: int | None


def _count_facilities(categories: dict | None) -> int | None:
    if not categories:
        return None
    return sum(int(v) for v in categories.values())


def _meta_from(path: Path, payload: dict) -> SampleMeta:
    props = payload.get("properties", {})
    # 有覆盖数据时按地图同一套方格重算分数，列表和打开后的报告一致。
    # 连报告都没有的空快照保持空白，不凭路网单独造一个等级。
    report = (
        build_report(
            props, props.get("coverage"), props.get("blindspots"), polygon_of(payload)
        )
        if props.get("report") or props.get("coverage")
        else {}
    )
    coverage = props.get("coverage") or {}
    return SampleMeta(
        id=path.stem,
        name=props.get("name", path.stem),
        minutes=float(props.get("minutes", 15)),
        center=props.get("center", {}),
        area_km2=float(props.get("area_km2", 0.0)),
        mean_radius_m=float(props.get("mean_radius_m", 0.0)),
        compactness=float(props.get("compactness", 0.0)),
        generated_at=props.get("generated_at"),
        grade=report.get("grade"),
        total=report.get("total"),
        area_ratio=report.get("area_ratio"),
        facilities_in=_count_facilities(coverage.get("categories") or report.get("categories")),
        facilities_nearby=_count_facilities(coverage.get("nearby_categories")),
    )


def list_samples() -> list[SampleMeta]:
    if not SAMPLES_DIR.exists():
        return []
    out: list[SampleMeta] = []
    for path in sorted(SAMPLES_DIR.glob("isochrone-*.geojson")):
        try:
            out.append(_meta_from(path, json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError, ValueError):
            continue  # 单个快照损坏不应让整个列表接口失败
    return out


def load_sample(sample_id: str) -> dict | None:
    # 拼接前先剥掉路径分隔符，避免 ../ 之类的路径穿越
    safe = Path(sample_id).name
    path = SAMPLES_DIR / f"{safe}.geojson"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
