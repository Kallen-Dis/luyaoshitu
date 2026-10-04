"""路口延误校正与施工围挡。

**为什么要校正。** 2026-09-24 在曹杨中心点实测：步行批量算路（routematrix）返回的
duration 恒等于路线距离 ÷ 约 1.17 m/s，不含任何过街或路口等待；而同一起终点的
步行路线规划（directionlite）在「过马路」「主干道」这类步骤里带额外秒数，
单次过马路约 +30 s，一条 1.5~1.8 公里的路线累计多出 110~190 s。
等时圈只用前者，会把红绿灯与天桥的代价整个漏掉——这正是命题点名的痛点。

**怎么校正，且不把配额打爆。** 批量算路便宜（一次 100 个点对），路线规划一次只算一条。
故仍用批量算路铺满 36 × 7 个采样点，只给**每个方向补一条路线**：

1. 以批量算路的中位步速 v0 为基准，路线每一步的额外等待 = max(0, 耗时 − 距离 / v0)；
2. 沿路线距离累加成「延误剖面」D(s)，步内按距离线性分摊；
3. 同方向较近的采样点近似视为走这条路线的前段，校正耗时 t' = t + D(d)，d 为该点的路网距离；
4. 用 t' 重新插值边界。

**施工围挡。** 两个接口都不接收避让区域，百度也没有面向个人开发者的围挡数据，
故由用户在地图上标注（圆心 + 半径）。路线折线第一次进入围挡的位置记为受阻点，
同方向路网距离超过它的采样点按「受阻」处理，射线在此截断。这是保守判定：
接口无法绕开围挡重新规划，真实情况可能存在绕行，需要现场复核。
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Any

from .geometry import bearing_deg, first_entry_along, polyline_length_m

# 路线步骤说明里表示过街的词。只用于计数展示；延误秒数取的是步骤里实测的额外时间，
# 不依赖这些词——不带「过马路」字样的主干道路段同样可能有路口等待。
CROSSING_WORDS: tuple[tuple[str, str], ...] = (
    ("天桥", "天桥"),
    ("地下通道", "地道"),
    ("地道", "地道"),
    ("人行横道", "过街"),
    ("斑马线", "过街"),
    ("过马路", "过街"),
    ("到斜对面", "过街"),
)

# 批量算路步速取不到时的兜底值（实测中位数）
DEFAULT_BASE_SPEED = 1.17

# 单次标注的围挡数上限与半径范围。圆太大就不是「围挡」而是整片封区，
# 应该直接换一个中心点重算。
MAX_CLOSURES = 20
MIN_CLOSURE_RADIUS_M = 10.0
MAX_CLOSURE_RADIUS_M = 300.0


@dataclass(frozen=True)
class Closure:
    """用户标注的施工围挡：以圆近似。"""

    lat: float
    lng: float
    radius_m: float
    label: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "lat": round(self.lat, 6),
            "lng": round(self.lng, 6),
            "radius_m": round(self.radius_m, 1),
            "label": self.label,
        }

    def as_circle(self) -> tuple[float, float, float]:
        return (self.lat, self.lng, self.radius_m)


def closures_from(raw: list[dict[str, Any]] | None) -> tuple[Closure, ...]:
    """从请求体或快照里恢复围挡列表，越界的半径夹到合法范围。"""
    out: list[Closure] = []
    for item in (raw or [])[:MAX_CLOSURES]:
        try:
            lat = float(item["lat"])
            lng = float(item["lng"])
            radius = float(item.get("radius_m") or 50.0)
        except (KeyError, TypeError, ValueError):
            continue
        radius = max(MIN_CLOSURE_RADIUS_M, min(MAX_CLOSURE_RADIUS_M, radius))
        out.append(Closure(lat, lng, radius, str(item.get("label") or "")[:40]))
    return tuple(out)


@dataclass
class DelayProfile:
    """一条步行路线的延误剖面：沿路线走到 s 米时，比匀速 v0 多花了多少秒。"""

    knots_s: list[float]
    knots_delay: list[float]
    length_m: float
    crossings: list[tuple[float, str]] = field(default_factory=list)
    # 路线第一次进入围挡时已走过的路网距离（米）；None 表示未穿过任何围挡
    blocked_at_m: float | None = None

    @property
    def total_delay_s(self) -> float:
        return self.knots_delay[-1] if self.knots_delay else 0.0

    def delay_at(self, s: float) -> float:
        """路线距离 s 处的累计等待。超出路线长度时按平均延误率外推。"""
        if s <= 0 or not self.knots_s:
            return 0.0
        if s >= self.length_m:
            if self.length_m <= 0:
                return 0.0
            return self.total_delay_s * s / self.length_m
        i = bisect.bisect_right(self.knots_s, s)
        s0, s1 = self.knots_s[i - 1], self.knots_s[i]
        d0, d1 = self.knots_delay[i - 1], self.knots_delay[i]
        if s1 <= s0:
            return d1
        return d0 + (d1 - d0) * (s - s0) / (s1 - s0)

    def crossings_before(self, s: float) -> int:
        return sum(1 for pos, _ in self.crossings if pos <= s)


def _crossing_kind(text: str) -> str | None:
    for word, kind in CROSSING_WORDS:
        if word in text:
            return kind
    return None


def build_profile(
    route: dict[str, Any],
    base_speed: float,
    closures: tuple[Closure, ...] = (),
) -> DelayProfile:
    """由一条路线的分段结果构造延误剖面，并判定是否穿过围挡。"""
    speed = base_speed if base_speed > 0 else DEFAULT_BASE_SPEED
    knots_s = [0.0]
    knots_delay = [0.0]
    crossings: list[tuple[float, str]] = []
    points: list[tuple[float, float]] = []
    s = 0.0
    delay = 0.0
    for step in route.get("steps") or []:
        dist = max(0.0, float(step.get("distance_m") or 0.0))
        dur = max(0.0, float(step.get("duration_s") or 0.0))
        delay += max(0.0, dur - dist / speed)
        s += dist
        knots_s.append(s)
        knots_delay.append(delay)
        kind = _crossing_kind(f"{step.get('instruction', '')}{step.get('turn_type', '')}")
        if kind:
            # 步骤说明描述的是这一步末尾的动作（「走 40 米，过马路左转」）
            crossings.append((s, kind))
        for lat, lng in step.get("path") or []:
            points.append((float(lat), float(lng)))

    length = float(route.get("distance_m") or 0.0) or s
    blocked_at: float | None = None
    if closures and len(points) >= 2:
        entry = first_entry_along(points, [c.as_circle() for c in closures])
        if entry is not None:
            geo = polyline_length_m(points)
            # 折线几何长度与百度给的路线距离略有出入，按比例折回路网距离口径
            blocked_at = entry * (length / geo) if geo > 0 else entry
    return DelayProfile(
        knots_s=knots_s,
        knots_delay=knots_delay,
        length_m=length,
        crossings=crossings,
        blocked_at_m=blocked_at,
    )


@dataclass(frozen=True)
class RefineOptions:
    """等时圈精细化选项。delay 校正过街等待；closures 为用户标注的围挡。"""

    delay: bool = True
    closures: tuple[Closure, ...] = ()

    @property
    def needs_routes(self) -> bool:
        return self.delay or bool(self.closures)


@dataclass
class RefineResult:
    """每个方向的路线剖面，以及汇总统计。热力网格的耗时校正也读它。"""

    bearings: list[float]
    profiles: list[DelayProfile | None]
    base_speed: float
    delay_applied: bool
    closures: tuple[Closure, ...]
    routes_requested: int = 0
    routes_ok: int = 0
    raw_area_m2: float = 0.0
    area_m2: float = 0.0
    boundary_crossings: int = 0
    closure_rays: int = 0
    # 每个方向实际取的路线与终点。写进结果里作复测巡检的基线：
    # 下次跳过缓存重取同一起终点的路线，比一比有没有变长、在哪里分岔
    routes: list[dict[str, Any] | None] = field(default_factory=list)
    targets: list[tuple[float, float] | None] = field(default_factory=list)

    def _neighbors(self, bearing: float) -> list[tuple[int, float]]:
        """方位角落在哪两条射线之间，返回 (射线下标, 角度权重)。"""
        n = len(self.bearings)
        if n == 0:
            return []
        step = 360.0 / n
        pos = (bearing % 360.0) / step
        i = int(pos) % n
        j = (i + 1) % n
        w = pos - int(pos)
        return [(i, 1.0 - w), (j, w)]

    def adjust(self, bearing: float, distance_m: float) -> tuple[float, bool, bool]:
        """某方位、某路网距离处的点：返回 (补回的等待秒数, 是否受围挡阻断, 是否有剖面可用)。

        等待按相邻两条射线的剖面按角度加权；围挡只看最近的那条射线——
        受阻是「这条路线被挡住」的结论，不宜在两个方向之间插值。
        """
        pairs = self._neighbors(bearing)
        usable = [(self.profiles[i], w) for i, w in pairs if self.profiles[i] is not None]
        delay = 0.0
        if self.delay_applied and usable:
            weight = sum(w for _, w in usable)
            if weight > 0:
                delay = sum(p.delay_at(distance_m) * w for p, w in usable) / weight
            else:
                delay = usable[0][0].delay_at(distance_m)
        blocked = False
        if pairs:
            nearest = max(pairs, key=lambda x: x[1])[0]
            prof = self.profiles[nearest]
            if prof is not None and prof.blocked_at_m is not None:
                blocked = distance_m >= prof.blocked_at_m
        return delay, blocked, bool(usable)

    def adjust_point(
        self,
        center: tuple[float, float],
        lat: float,
        lng: float,
        distance_m: float,
    ) -> tuple[float, bool, bool]:
        return self.adjust(bearing_deg(center[0], center[1], lat, lng), distance_m)

    def as_dict(self) -> dict[str, Any]:
        ok = [p for p in self.profiles if p is not None]
        length_km = sum(p.length_m for p in ok) / 1000.0
        total_delay = sum(p.total_delay_s for p in ok)
        return {
            "applied": self.delay_applied and bool(ok),
            "source": "百度步行路线规划（directionlite）每个方向一条路线，"
            "步骤耗时超出匀速步行的部分按路线距离分摊，补到批量算路的耗时上",
            "base_speed_m_per_s": round(self.base_speed, 3),
            "routes_requested": self.routes_requested,
            "routes_ok": self.routes_ok,
            "delay_per_km_s": round(total_delay / length_km, 1) if length_km > 0 else None,
            "boundary_crossings": self.boundary_crossings,
            "raw_area_km2": round(self.raw_area_m2 / 1e6, 4),
            "area_km2": round(self.area_m2 / 1e6, 4),
            "area_ratio": (round(self.area_m2 / self.raw_area_m2, 3) if self.raw_area_m2 else None),
            "closures": [c.as_dict() for c in self.closures],
            "closure_rays": self.closure_rays,
            "note": (
                "等待秒数来自百度路线模型，是统计意义上的过街与路口延误，"
                "不是当前信号灯的实时相位；同方向较近的点按这条路线的前段近似。"
                "围挡为用户标注，路线穿过即按受阻处理，"
                "接口不能绕开围挡重新规划，实际可能存在绕行。"
            ),
        }
