"""出行方式：等时圈用哪张路网、考虑哪些因素。

命题默认是 15 分钟步行生活圈。真实出行并不只有步行——骑行走非机动车道，
驾车走车行道且受实时路况影响。百度批量算路按出行方式给出不同路网与耗时模型，
本模块把这些差异收成可切换的配置，而不是在算法里写死步行半径。

接口能提供和不能提供的，必须分开写清：

- **能进入耗时的**：道路选择（人行道 / 非机动车道 / 车行道）、驾车实时路况（tactics=11 相对 13）。
- **步行批量算路做不到、要另想办法的**：过街与路口等待。2026-09-24 实测步行批量算路的
  duration 恒等于路线距离 ÷ 约 1.17 m/s，不含任何等待；而步行路线规划（directionlite）
  在过马路、主干道路段的步骤里带额外秒数（单次过马路约 +30 s）。isochrone/refine.py
  用每个方向一条路线的额外秒数校正批量算路的耗时。
- **接口完全做不到的**：施工围挡。两个接口都不接收避让区域，故由用户在地图上标注，
  路线穿过围挡的方向按受阻处理（见 isochrone/refine.py）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 15 分钟直线极限：速度 × 900 秒。采样上界略放宽，避免通畅方向顶到格子就饱和。
WALK_RADII_M = (200, 400, 600, 800, 1000, 1200, 1400)
RIDE_RADII_M = (400, 800, 1400, 2000, 2800, 3600, 4500)
DRIVE_RADII_M = (800, 1600, 2800, 4200, 6000, 8000, 11000)


@dataclass(frozen=True)
class TravelMode:
    id: str
    label: str
    endpoint: str
    cache_ns: str
    speed_m_per_s: float
    radii_m: tuple[float, ...]
    # 官方起点×终点乘积上限：步行 / 骑行 50，驾车 100。
    matrix_product_limit: int = 100
    extra_params: dict[str, str | int] = field(default_factory=dict)
    uses_traffic: bool = False
    # 网格盲区是「居民点步行 1 公里能否到达设施」，只对步行等时圈有命题意义。
    # 驾车圈面积大一个数量级，150 米网格会把算路点对打爆。
    allow_grid_blindspots: bool = False
    factors: tuple[str, ...] = ()

    def as_public(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "speed_m_per_s": self.speed_m_per_s,
            "uses_traffic": self.uses_traffic,
            "allow_grid_blindspots": self.allow_grid_blindspots,
            "factors": list(self.factors),
        }


WALK = TravelMode(
    id="walk",
    label="步行",
    endpoint="/routematrix/v2/walking",
    cache_ns="walk",
    speed_m_per_s=1.2,
    radii_m=WALK_RADII_M,
    matrix_product_limit=50,
    allow_grid_blindspots=True,
    factors=(
        "人行道与过街设施（人行横道、天桥、地道），不能走机动车道或高架",
        "铁路、河道、围墙表现为不可达，射线在此截断",
        "批量算路的耗时只是路线距离 ÷ 约 1.17 m/s，不含过街等待；"
        "每个方向另取一条步行路线，把其中的过街与路口等待按路线距离补回耗时",
        "施工围挡可在地图上标注：路线穿过围挡的方向按受阻处理",
    ),
)

RIDE = TravelMode(
    id="ride",
    label="骑行",
    endpoint="/routematrix/v2/riding",
    cache_ns="ride",
    speed_m_per_s=4.0,
    radii_m=RIDE_RADII_M,
    matrix_product_limit=50,
    extra_params={"riding_type": 0},
    factors=(
        "非机动车道与可骑行道路，绕开步行阶梯与封闭小区内部",
        "速度按自行车而非电动车；15 分钟直线极限约 3.6 公里",
        "路口等待已计入耗时，同样不单列灯控秒数",
    ),
)

DRIVE_FREE = TravelMode(
    id="drive",
    label="驾车（畅通路况）",
    endpoint="/routematrix/v2/driving",
    cache_ns="drive",
    speed_m_per_s=10.0,
    radii_m=DRIVE_RADII_M,
    extra_params={"tactics": 13},
    factors=(
        "车行道路网，含高架与快速路",
        "tactics=13：距离较短且不考虑实时路况，相当于路网完全畅通时的预计耗时",
        "可与「实时路况」对照，差额即拥堵造成的时间损失",
    ),
)

DRIVE_TRAFFIC = TravelMode(
    id="drive_traffic",
    label="驾车（实时路况）",
    endpoint="/routematrix/v2/driving",
    cache_ns="drive_traffic",
    speed_m_per_s=10.0,
    radii_m=DRIVE_RADII_M,
    extra_params={"tactics": 11},
    uses_traffic=True,
    factors=(
        "车行道路网，偏好为多数人常走的常规路线（tactics=11）",
        "耗时计算考虑实时路况；批量算路不含道路阻断信息干预",
        "路况缓存 10 分钟内视为实时；接口失败时用更早的路况兜底并注明是多久以前的",
        "红绿灯等待被路况与路口模型吸收进 duration，不能拆成独立图层",
    ),
)

MODES: dict[str, TravelMode] = {
    WALK.id: WALK,
    RIDE.id: RIDE,
    DRIVE_FREE.id: DRIVE_FREE,
    DRIVE_TRAFFIC.id: DRIVE_TRAFFIC,
}

DEFAULT_MODE_ID = WALK.id


def get_mode(mode_id: str | None) -> TravelMode:
    if not mode_id:
        return WALK
    mode = MODES.get(mode_id)
    if mode is None:
        known = "、".join(MODES)
        raise ValueError(f"未知出行方式 {mode_id!r}，可选：{known}")
    return mode
