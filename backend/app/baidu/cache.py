"""按坐标量化的磁盘缓存。

地点检索的日配额是全项目最紧的约束（见 reports/quota-report.md 第四节），
一次耗尽就得等次日 0 点。因此缓存不是性能优化，而是功能可用性的前提：
它让重复演示零消耗，也让配额耗尽后的重跑能断点续上。

量化策略：坐标按 cache_grid_m（默认 50 米）对齐到网格。步行可达性在 50 米
尺度上的差异远小于 POI 检索的粒度，这个近似换来的缓存命中率提升非常划算。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

# 纬度 1 度约 111320 米。经度方向随纬度收缩，但量化只需稳定一致，不必严格等距。
METERS_PER_DEGREE = 111_320.0


class DiskCache:
    def __init__(self, root: Path, grid_m: float = 50.0) -> None:
        self._root = root
        self._grid_deg = grid_m / METERS_PER_DEGREE

    def _quantize(self, value: float) -> int:
        return round(value / self._grid_deg)

    def _path(self, namespace: str, key_parts: list[Any]) -> Path:
        raw = "|".join(str(p) for p in key_parts)
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]
        return self._root / namespace / f"{digest}.json"

    def key_for_point(self, namespace: str, lat: float, lng: float, *extra: Any) -> Path:
        return self._path(namespace, [self._quantize(lat), self._quantize(lng), *extra])

    def key_for_pair(
        self, namespace: str, o_lat: float, o_lng: float, d_lat: float, d_lng: float, *extra: Any
    ) -> Path:
        return self._path(
            namespace,
            [
                self._quantize(o_lat), self._quantize(o_lng),
                self._quantize(d_lat), self._quantize(d_lng),
                *extra,
            ],
        )

    @staticmethod
    def read(path: Path) -> Any | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None  # 缓存损坏时静默回源，不应让缓存问题拖垮主流程

    @staticmethod
    def write(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)  # 原子替换，避免并发写出半截文件
