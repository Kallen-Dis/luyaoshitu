"""复用共享连接与熔断，隔离出行精确坐标缓存和居民内存缓存。"""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from ..baidu.cache import DiskCache
from ..baidu.client import BaiduMapClient, request_observer
from .budget import TripBudget, Usage


class ExactCache:
    def __init__(self, client: BaiduMapClient, memory_only: bool, usage: Usage) -> None:
        self.client = client
        self.memory_only = memory_only
        self.usage = usage
        self.root = client._s.cache_dir / "trip"
        if not hasattr(client, "_trip_memory"):
            client._trip_memory = OrderedDict()
        self.memory: OrderedDict = client._trip_memory
        # TTL 不只是忽略过期条目：运行中的服务每天清理一次中心位置缓存。
        if time.time() - getattr(client, "_trip_pruned_at", 0) > 86400:
            root = self.root.resolve()
            for path in self.root.rglob("*.json"):
                try:
                    if (
                        path.resolve().is_relative_to(root)
                        and time.time() - path.stat().st_mtime > client._s.cache_ttl_s
                    ):
                        path.unlink(missing_ok=True)
                except OSError:
                    # 别的 worker 可能同时替换/清理；缓存维护不阻断查询。
                    continue
            client._trip_pruned_at = time.time()

    def key_for_pair(self, namespace: str, a: float, b: float, c: float, d: float, *extra) -> Path:
        raw = f"{a:.6f}|{b:.6f}|{c:.6f}|{d:.6f}"
        return self.root / namespace / f"{hashlib.sha256(raw.encode()).hexdigest()}.json"

    def key_for_point(self, namespace: str, lat: float, lng: float, *extra) -> Path:
        raw = json.dumps([f"{lat:.6f}", f"{lng:.6f}", *extra], ensure_ascii=False)
        return self.root / namespace / f"{hashlib.sha256(raw.encode()).hexdigest()}.json"

    def read(self, path: Path, max_age_s: float | None = None) -> Any | None:
        if not self.memory_only:
            value = DiskCache.read(path, max_age_s)
        else:
            hit = self.memory.get(str(path))
            if hit is None:
                return None
            at, value = hit
            if time.time() - at > (max_age_s or self.client._s.cache_ttl_s):
                del self.memory[str(path)]
                return None
            self.memory.move_to_end(str(path))
        # 某些旧条目只有距离、没有可用折线；补取时必须真正重查，不能命中 30 天的空路线。
        if (
            value is not None
            and path.parent.name == "route_walk"
            and (
                not isinstance(value, dict)
                or sum(len(step.get("path") or []) for step in value.get("steps") or []) < 2
            )
        ):
            return None
        if value is not None:
            self.usage.cache_hits += 1
        return value

    def write(self, path: Path, payload: Any) -> None:
        if not self.memory_only:
            path.parent.mkdir(parents=True, exist_ok=True)
            # 不共用 .tmp 文件，多个请求同时填同一个精确坐标键也可原子替换。
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
            ) as out:
                tmp = Path(out.name)
                json.dump(payload, out, ensure_ascii=False)
            try:
                tmp.replace(path)
            finally:
                tmp.unlink(missing_ok=True)
            return
        self.memory[str(path)] = (time.time(), payload)
        self.memory.move_to_end(str(path))
        while len(self.memory) > self.client._s.trip_memory_entries:
            self.memory.popitem(last=False)

    def age_s(self, path: Path) -> float | None:
        if not self.memory_only:
            return DiskCache.age_s(path)
        hit = self.memory.get(str(path))
        return max(0.0, time.time() - hit[0]) if hit else None


class TripRoutingClient(BaiduMapClient):
    def __init__(
        self, base: BaiduMapClient, budget: TripBudget, usage: Usage, memory_only: bool
    ) -> None:
        super().__init__(base._s)
        self.base = base
        self.budget = budget
        self.usage = usage
        self._cache = ExactCache(base, memory_only, usage)
        self._route_gate = base._route_gate
        self._matrix_gate = base._matrix_gate

    async def _request(self, endpoint: str, params: dict, cost: float = 1.0) -> dict:
        token = request_observer.set(lambda e, p: self.budget.observe(self.usage, e, p))
        try:
            return await self.base._request(endpoint, params, cost)
        finally:
            request_observer.reset(token)

    def exhausted(self, endpoint: str):
        return self.base.exhausted(endpoint)
