"""示例数据仓库。

地点检索的免费日配额实测仅约 150 次（见 reports/quota-report.md 4.1.1），
评审方连续演示两三次就会耗尽。因此预生成的样例快照随仓库分发，
应用默认从快照读取，实时计算仅作为可选路径。

这既是对配额约束的必要应对，也正好满足交付要求中
"配置好示例数据，保证评审方能快速跑通演示"。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import PROJECT_ROOT

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


def _meta_from(path: Path, payload: dict) -> SampleMeta:
    props = payload.get("properties", {})
    return SampleMeta(
        id=path.stem,
        name=props.get("name", path.stem),
        minutes=float(props.get("minutes", 15)),
        center=props.get("center", {}),
        area_km2=float(props.get("area_km2", 0.0)),
        mean_radius_m=float(props.get("mean_radius_m", 0.0)),
        compactness=float(props.get("compactness", 0.0)),
        generated_at=props.get("generated_at"),
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
