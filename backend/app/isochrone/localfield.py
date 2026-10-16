"""射线先验上的局部耗时校正。计算网格不是实测网格，也不代表底层道路。

使用正权重观测、面积积分的修正梯度和回归先验三项平方误差，并约束局部取值。
缺测不参与拟合；强路线跳变限制平滑，不定位真实障碍。各时间阈值共享一个场。
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass

from .fieldmesh import FieldMesh, barycentric, segment_crosses_ring
from .tin import MeshContour, Vertex, _clip_time, delaunay, inside_ring, merge_fragments, ring_area

# 仅用于边界分类约束与发布检查，不修改原始测时，也不是接口误差上限。
BOUNDARY_TOLERANCE_S = 30.0


def local_envelopes(vertices, base, observations, spacing):
    """节点只能取附近路线及射线先验的取值范围，不允许最小二乘外推峰值。

    边界不是 900 秒截断，真实超时观测同样进入上下界。没有路线的节点
    仍可被已有支持范围内的平滑影响；不把矩阵异常当作放宽界限的依据。
    """
    routes = [o for o in observations if o.protected]
    bounds, nearest = [], []
    for v, initial in zip(vertices, base, strict=True):
        nearby = sorted((math.hypot(v.x - o.x, v.y - o.y), j) for j, o in enumerate(routes))
        local = [routes[j].seconds for d, j in nearby if d <= math.sqrt(2) * spacing]
        bounds.append((min(initial, *local), max(initial, *local)) if local else (0, math.inf))
        nearest.append(routes[nearby[0][1]] if nearby and nearby[0][0] <= spacing else None)
    return bounds, nearest


def route_jump(a, b):
    """仅真实路线的距离和耗时同时明显跳变时，断开残差平滑边。

    这只是计算图上的耦合限制，不能解释为障碍的位置。
    """
    if a is None or b is None or a.distance is None or b.distance is None:
        return False
    gap = math.hypot(a.x - b.x, a.y - b.y)
    return (
        10 <= gap <= 150
        and abs(a.distance - b.distance) > 2 * gap + 60
        and abs(a.seconds - b.seconds) > 120
    )


def correction_support(vertices, observations, target=900, scale=1.0):
    """慢终点局限于一个计算格附近，不让单个慢点扩散到附近快路。

    这些是保守的模型影响半径，不是实测精度。无路线证据处严格回到射线先验。
    """
    routes = [o for o in observations if o.protected]
    radius = []
    for o in routes:
        corroborated = any(
            25 <= math.hypot(o.x - q.x, o.y - q.y) <= 150
            and o.seconds > target + BOUNDARY_TOLERANCE_S
            and q.seconds > target + BOUNDARY_TOLERANCE_S
            and abs(o.seconds - q.seconds) <= 120
            for q in routes
        )
        radius.append(((75.0 if corroborated else 50.0) if o.seconds > target else 125.0) * scale)
    support = []
    for v in vertices:
        best = 0.0
        for o, r in zip(routes, radius, strict=True):
            d = math.hypot(v.x - o.x, v.y - o.y)
            u = min(1.0, max(0.0, (r - d) / max(r - 15.0, 1)))
            best = max(best, u * u * (3 - 2 * u))
        support.append(best)
    return support


@dataclass(frozen=True)
class Observation:
    x: float
    y: float
    seconds: float
    weight: float = 2.0
    protected: bool = False
    distance: float | None = None


@dataclass(frozen=True)
class RouteBound:
    path: tuple[tuple[float, float], ...]
    seconds: float
    distance: float | None = None


def evidence_patches(observations, routes, target, *, reachable=False):
    """仅在相邻同侧实测点围成的小三角区域保持分类，不跨可信可达路线。

    这是有证据的局部插值条件，不证明区域内每平方米都走不到。
    临界点、稀疏点及耗时/距离跳变不支持连续排除区。
    """
    grouped = {}
    for o in observations:
        if o.protected:
            grouped.setdefault((o.x, o.y), []).append(o)
    positions = sorted(grouped)
    times = [min(o.seconds for o in grouped[p]) for p in positions]
    patches = []
    for indices in delaunay(positions):
        face = [positions[i] for i in indices]
        gaps = [math.dist(a, b) for a, b in zip(face, (*face[1:], face[0]), strict=True)]
        if min(gaps) < 25 or max(gaps) > 100:
            continue
        evidence = [o for p in face for o in grouped[p]]
        if max(o.seconds for o in evidence) - min(o.seconds for o in evidence) > 120:
            continue
        if any(
            a.distance is not None
            and b.distance is not None
            and abs(a.distance - b.distance) > 2 * math.dist((a.x, a.y), (b.x, b.y)) + 60
            for i, a in enumerate(evidence)
            for b in evidence[i + 1 :]
        ):
            continue
        limits = []
        for t in sorted({t for t in (300, 600, target) if t <= target}):
            if reachable:
                if max(times[i] for i in indices) >= t - BOUNDARY_TOLERANCE_S:
                    continue
                if any(
                    o.protected and o.seconds > t and inside_ring(o.x, o.y, face)
                    for o in observations
                ):
                    continue
                limits.append(max(times[i] for i in indices))
                continue
            if min(times[i] for i in indices) <= t + BOUNDARY_TOLERANCE_S:
                continue
            if any(
                o.seconds <= t + BOUNDARY_TOLERANCE_S and inside_ring(o.x, o.y, face)
                for o in observations
                if o.protected
            ):
                continue
            if any(
                r.seconds <= t
                and any(
                    segment_crosses_ring(a, b, face)
                    for a, b in zip(r.path, r.path[1:], strict=False)
                )
                for r in routes
            ):
                continue
            limits.append(t + 0.1)
        if not limits:
            continue
        patches.append((face, min(limits) if reachable else max(limits)))
    return patches


def patch_floors_at(vertices, patches, *, upper=False):
    floors = [math.inf if upper else 0.0] * len(vertices)
    for face, floor in patches:
        for k, v in enumerate(vertices):
            w = barycentric((v.x, v.y), *face)
            if w is not None and min(w) >= -1e-8:
                floors[k] = min(floors[k], floor) if upper else max(floors[k], floor)
    return floors


def corroborated_patches(vertices, observations, routes, target):
    patches = evidence_patches(observations, routes, target)
    return patch_floors_at(vertices, patches), len(patches)


class RayPrior:
    """方向采样提供径向耗时，正式射线边界校准目标阈值的位置。"""

    def __init__(self, ring, samples, target):
        self.target = target
        self.ring = ring
        groups = {}
        for x, y, t, _ in samples:
            if t is None or not math.isfinite(t) or math.hypot(x, y) < 1:
                continue
            angle = round(math.atan2(y, x) % math.tau, 4)
            groups.setdefault(angle, []).append((math.hypot(x, y), t))
        self.angles = sorted(groups)
        self.profiles = []
        for a in self.angles:
            profile = [(0.0, 0.0)]
            for radius, t in sorted(groups[a]):
                profile.append((radius, max(profile[-1][1] + 0.01, t)))
            self.profiles.append(profile)

    def boundary(self, x, y):
        radius = math.hypot(x, y)
        if radius < 1e-9:
            return 1.0
        ux, uy = x / radius, y / radius
        best = math.inf
        for a, b in zip(self.ring, [*self.ring[1:], self.ring[0]], strict=True):
            dx, dy = b[0] - a[0], b[1] - a[1]
            det = ux * dy - uy * dx
            if abs(det) < 1e-10:
                continue
            r = (a[0] * dy - a[1] * dx) / det
            s = (a[0] * uy - a[1] * ux) / det
            if r > 0 and -1e-8 <= s <= 1 + 1e-8:
                best = min(best, r)
        return best if math.isfinite(best) else max(math.hypot(*p) for p in self.ring)

    def profile_time(self, profile, radius, boundary):
        crossing = profile[-1][0] * self.target / max(profile[-1][1], 1)
        for a, b in zip(profile, profile[1:], strict=False):
            if a[1] <= self.target <= b[1]:
                crossing = a[0] + (b[0] - a[0]) * (self.target - a[1]) / (b[1] - a[1])
                break
        scaled = radius * crossing / max(boundary, 1)
        for a, b in zip(profile, profile[1:], strict=False):
            if scaled <= b[0]:
                return a[1] + (b[1] - a[1]) * (scaled - a[0]) / (b[0] - a[0])
        return profile[-1][1] + (scaled - profile[-1][0]) * max(
            0.5, (profile[-1][1] - profile[-2][1]) / (profile[-1][0] - profile[-2][0])
        )

    def __call__(self, x, y):
        r = math.hypot(x, y)
        if r < 1e-9:
            return 0.0
        boundary = self.boundary(x, y)
        if not self.angles:
            return self.target * r / max(boundary, 1)
        angle = math.atan2(y, x) % math.tau
        right = bisect.bisect_right(self.angles, angle) % len(self.angles)
        left = (right - 1) % len(self.angles)
        span = (self.angles[right] - self.angles[left]) % math.tau
        f = ((angle - self.angles[left]) % math.tau) / span if span else 0.0
        a = self.profile_time(self.profiles[left], r, boundary)
        b = self.profile_time(self.profiles[right], r, boundary)
        return a * (1 - f) + b * f


def fit_field(
    ring,
    samples,
    observations,
    target=900.0,
    spacing=50.0,
    routes=(),
    refine=False,
    rollback_xy=(),
    clues=(),
    support_scale=1.0,
    domain=None,
):
    """实际终点局部插点，整段路线作上限；无法满足的约束显式拒绝，不全图加密。"""
    prior = RayPrior(ring, samples, target)
    extent = max(max(math.hypot(*p) for p in ring) + 250, 400)
    observation_count = len(observations)
    observations = sorted(
        {o for o in observations if math.hypot(o.x, o.y) <= extent - spacing},
        key=lambda o: (o.x, o.y, o.seconds, o.weight),
    )
    routes = sorted(set(routes), key=lambda r: (r.seconds, r.path))
    mesh = FieldMesh(extent, spacing)
    initial_count = len(mesh.points)
    # 加密只发生在可靠点附近；真实测点自身始终是节点，不贴到格心。
    # 局部节点不代表测量精度：常规12.5米插点，另用6.25米插点作数值对照。
    # 两层独立求解，不能拿同一层重复计算冒充收敛检查。
    for radius in (12.5, 6.25) if refine else (12.5,):
        for o in observations:
            if o.protected:
                for dx, dy in (
                    (-radius, -radius),
                    (-radius, radius),
                    (radius, -radius),
                    (radius, radius),
                ):
                    mesh.insert((o.x + dx, o.y + dy))
    for o in observations:
        if o.protected:
            mesh.insert((o.x, o.y))
    # 为路径与慢点之间保留自由节点，避免三角形的三个实测慢节点锁死途经快路。
    # 只插入已有路径几何及其穿越位置，不把整张计算网格改成 25 米。
    for route in routes:
        for point in route.path:
            if max(abs(point[0]), abs(point[1])) < mesh.limit - spacing:
                mesh.insert(point)
    mesh_flips = mesh.improve_quality()
    patches = evidence_patches(observations, routes, target)
    reachable_patches = evidence_patches(observations, routes, target, reachable=True)
    patch_edges = {
        tuple(sorted((a, b)))
        for face, _ in [*patches, *reachable_patches]
        for a, b in zip(face, (*face[1:], face[0]), strict=True)
    }
    for a, b in sorted(patch_edges):
        mesh.conform_segment(a, b)
    # 下限必须覆盖证据三角形的整片区域。只给内部节点赋下限会让跨界
    # 三角形从外部快节点插入可达细缝，加密甚至会将同一慢区割成两个洞。
    # 先验边界沿计算三角形的边穿过，避免零观测也生成微小差集片区。
    # 这些节点只是基础耗时，附近有可靠证据时仍可移动边界。
    boundary = set()
    for point in ring:
        k = mesh.insert(point)
        if k is not None:
            boundary.add(k)
    for a, b in zip(ring, (*ring[1:], ring[0]), strict=True):
        crossings = [
            tuple(sum(mesh.points[k][axis] * w for k, w in weights) for axis in (0, 1))
            for weights in mesh.line_stencils(a, b)
        ]
        for point in crossings:
            k = mesh.insert(point)
            if k is not None:
                boundary.add(k)
    if domain is not None:
        from .domain import mesh_domain

        mesh_domain(mesh, domain)
    points = mesh.points
    vertices = [Vertex(x, y, 0, None) for x, y in points]
    base = [prior(x, y) for x, y in points]
    for k in boundary:
        base[k] = target
    support = correction_support(vertices, observations, target, support_scale)
    patch_floors = patch_floors_at(vertices, patches)
    patch_ceilings = patch_floors_at(vertices, reachable_patches, upper=True)
    patch_count = len(patches)
    for k, floor in enumerate(patch_floors):
        if floor:
            support[k] = 1.0
    bounds, anchors = local_envelopes(vertices, base, observations, spacing)
    path_constraints = {}
    invalid_segments = 0
    clipped_paths = 0
    water_path_conflicts = 0
    # 地区水面可能有数千坐标；复用同一局部投影，不能逐个路线段重新转换全量数据。
    water_xy = domain.xy_shape(domain.water) if domain is not None else None
    if water_xy is not None:
        from shapely.prepared import prep

        prepared_water = prep(water_xy)
    for route in routes:
        # 长直路段本身不是错误；只有占位几何或长度与路线报告明显不符才拒绝。
        path_length = sum(
            math.hypot(b[0] - a[0], b[1] - a[1])
            for a, b in zip(route.path, route.path[1:], strict=False)
        )
        geometry_ok = len(route.path) >= 3 or path_length <= 100
        if route.distance is not None:
            geometry_ok = geometry_ok and abs(path_length - route.distance) <= max(
                30, route.distance * 0.25
            )
        if not geometry_ok:
            invalid_segments += 1
            continue
        for a, b in zip(route.path, route.path[1:], strict=False):
            length = math.hypot(a[0] - b[0], a[1] - b[1])
            if length < 1e-6:
                continue
            if any(max(abs(x), abs(y)) >= mesh.limit for x, y in (a, b)):
                clipped_paths += 1
                continue
            if water_xy is not None:
                from shapely.geometry import LineString

                segment = LineString([a, b])
                if (
                    prepared_water.intersects(segment)
                    and segment.intersection(water_xy).length > 1e-6
                ):
                    # 水面仍从网格排除；路线可能经过未收录桥面或受坐标偏差影响。
                    # 该段不提供路径上限，保留冲突记录供局部核实，不否定其他陆地证据。
                    water_path_conflicts += 1
                    continue
            for weights in mesh.line_stencils(a, b):
                key = tuple((k, round(w, 10)) for k, w in weights)
                previous = path_constraints.get(key)
                if previous is None or previous[1] > route.seconds:
                    path_constraints[key] = weights, route.seconds
    constraints = list(path_constraints.values())
    # 实际路径上的约束可以修改涉及的三角形；不对道路做固定宽度缓冲。
    for weights, _ in constraints:
        for k, _ in weights:
            support[k] = 1.0
    rollback_keys = {(round(x, 6), round(y, 6)) for x, y in rollback_xy}
    rollback = {
        k for k, p in enumerate(points) if (round(p[0], 6), round(p[1], 6)) in rollback_keys
    }
    for k in rollback:
        support[k] = 0.0
    from .fieldenergy import regularization

    triangles = [face for face in mesh.faces if face is not None]
    diag, rows, mass, weakened = regularization(points, triangles, anchors, clues, route_jump)
    rhs = [0.0 for _ in vertices]
    prepared = []
    for o in observations:
        # 矩阵加插值等待与单点路线不是同一耗时观测。未核实点保留给风险选点，
        # 不因它恰好靠近某条路线，就获得推动正式边界的权力。
        if not o.protected:
            continue
        weights = mesh.stencil((o.x, o.y))
        if not weights:
            continue
        initial = sum(base[k] * a for k, a in weights)
        # 软拟合在原测时 ±30 秒内偏向先验，避免临界观测制造尖峰。
        # 这是正则化的工程死区，不是接口误差保证；硬分类继续按原测时检查。
        fitted = min(
            o.seconds + BOUNDARY_TOLERANCE_S, max(o.seconds - BOUNDARY_TOLERANCE_S, initial)
        )
        weight = o.weight
        for k, a in weights:
            diag[k] += weight * a * support[k] ** 2
            rhs[k] += weight * a * support[k] * (fitted - base[k])
        prepared.append((o, weights))
    residual = [0.0 for _ in vertices]
    # 钝角有限元单元不保证离散最大值原理。只观测到变快时不能凭平滑
    # 生出正修正，反之亦然；局部支持仍限制传播，不截断刚度矩阵系数。
    observed_deltas = [o.seconds - prior(o.x, o.y) for o in observations if o.protected]
    delta_low, delta_high = min([0.0, *observed_deltas]), max([0.0, *observed_deltas])
    center = mesh.stencil((0, 0))
    fixed = {k for k, w in center if w > 0.999999}
    change = 0.0
    for _iteration in range(3000):
        change = 0.0
        for k, row in enumerate(rows):
            if k in fixed or not support[k] or diag[k] <= 0:
                continue
            value = (rhs[k] - sum(c * residual[j] for j, c in row.items())) / diag[k]
            low, high = bounds[k]
            value = min(
                delta_high,
                (high - base[k]) / support[k],
                max(delta_low, (low - base[k]) / support[k], value),
            )
            change = max(change, abs(value - residual[k]))
            residual[k] = value
        if change < 0.025:
            break
    values = [max(0, a + s * b) for a, s, b in zip(base, support, residual, strict=True)]
    verified = [(o, w) for o, w in prepared if o.protected]
    thresholds = sorted({t for t in (300, 600, target) if t <= target})
    # 临近十五分钟不等于临近五/十分钟；每个展示阈值都独立保护明确分类。
    protected = [
        (o, w)
        for o, w in verified
        if any(abs(o.seconds - t) > BOUNDARY_TOLERANCE_S for t in thresholds)
    ]
    for k in fixed:
        values[k] = 0.0
    lower, upper = patch_floors, patch_ceilings
    for k in fixed:
        lower[k] = 0.0
        upper[k] = 0.0
    for k in rollback - fixed:
        # 圈外碎片只撤销“可达”分类；不把先验大耗时强加给已知较短路径。
        restored = min(base[k], target + 1.0)
        lower[k] = max(lower[k], restored)
        upper[k] = restored
    data_conflicts = sum(
        o.seconds > 1e-6 and any(k in fixed and w > 0.999999 for k, w in weights)
        for o, weights in verified
    )

    def on_path(o, path):
        for a, b in zip(path, path[1:], strict=False):
            dx, dy = b[0] - a[0], b[1] - a[1]
            length2 = dx * dx + dy * dy
            f = max(0, min(1, ((o.x - a[0]) * dx + (o.y - a[1]) * dy) / length2)) if length2 else 0
            if math.hypot(o.x - a[0] - f * dx, o.y - a[1] - f * dy) <= 1e-5:
                return True
        return False

    for o, weights in verified:
        # 实际可靠终点是节点，所以不会把高耗时分配到附近快路上。
        if len(weights) != 1:
            data_conflicts += 1
            continue
        k = weights[0][0]
        if k in fixed:
            # 不能用端点观测覆盖出发位置的零耗时，也不能静默忽略冲突来源。
            continue
        upper[k] = min(upper[k], o.seconds)
        # 同一有效起点的可靠测点保护明确分类，只约束实际节点，不向周围传播下限。
        # 与其他快路线冲突时由路径投影及响应冲突检查显式拒绝。
        for t in thresholds:
            contradicted = any(r.seconds <= t and on_path(o, r.path) for r in routes) or any(
                q.seconds <= t and math.hypot(o.x - q.x, o.y - q.y) < 1e-5 for q, _ in verified
            )
            if o.seconds > t + BOUNDARY_TOLERANCE_S and not contradicted:
                lower[k] = max(lower[k], t + 1.0)
    data_conflicts += sum(lo > hi for lo, hi in zip(lower, upper, strict=True))
    for k in range(len(values)):
        if lower[k] <= upper[k]:
            values[k] = min(upper[k], max(lower[k], values[k]))
        else:
            values[k] = lower[k]  # 冲突候选拒绝采用，不能为撤销修正而反判可靠测点。
    # 上限投影只降低值；超时终点下限不被覆盖，冲突不能靠平均消失。
    for _ in range(80):
        biggest = 0.0
        for weights, limit in constraints:
            excess = sum(values[k] * w for k, w in weights) - limit
            biggest = max(biggest, excess)
            if excess <= 1e-7:
                continue
            free = [(k, w) for k, w in weights if k not in fixed and values[k] > lower[k] + 1e-9]
            denominator = sum(w * w for _, w in free)
            if denominator > 1e-15:
                for k, w in free:
                    values[k] = max(lower[k], values[k] - excess * w / denominator)
        if biggest <= 1e-6:
            break
    from .microholes import regularize_micro_holes

    field, micro_holes = regularize_micro_holes(
        [Vertex(x, y, t, None) for (x, y), t in zip(points, values, strict=True)],
        triangles,
        observations,
        target,
        domain=domain,
        water_xy=water_xy,
        lower=lower,
        clues=clues,
        area_limit=min(500.0, abs(ring_area(ring)) * 0.001),
    )
    values = [v.t for v in field]
    path_violations = [
        max(0, sum(values[k] * w for k, w in weights) - limit) for weights, limit in constraints
    ]

    response_conflicts = [
        o
        for o, _ in protected
        if any(
            o.seconds > t + BOUNDARY_TOLERANCE_S
            and (
                any(r.seconds <= t and on_path(o, r.path) for r in routes)
                or any(
                    q.seconds <= t and math.hypot(o.x - q.x, o.y - q.y) < 1e-5 for q, _ in verified
                )
            )
            for t in thresholds
        )
    ]
    conflicts = [
        o
        for o, w in protected
        if any(
            o.seconds < t - BOUNDARY_TOLERANCE_S and sum(values[k] * a for k, a in w) > t
            for t in thresholds
        )
    ]
    edge_reachable = any(
        t <= target
        for (x, y), t in zip(points, values, strict=True)
        if max(abs(x), abs(y)) >= mesh.limit - 1e-6
    )
    quality = {
        "micro_hole_regularization": micro_holes,
        "solver_iterations": _iteration + 1,
        "solver_change_s": round(change, 4),
        "solver_converged": change < 0.025,
        "observed_correction_range_s": [delta_low, delta_high],
        "computational_spacing_m": spacing,
        "local_node_offsets_m": [12.5, 6.25] if refine else [12.5],
        "mesh_quality_flips": mesh_flips,
        "local_inserted_vertices": len(points) - initial_count,
        "observations": len(observations),
        "duplicate_or_outside_observations": observation_count - len(observations),
        "soft_observation_deadband_s": BOUNDARY_TOLERANCE_S,
        "prior_boundary_vertices": len(boundary),
        "corroborated_patch_triangles": patch_count,
        "corroborated_reachable_triangles": len(reachable_patches),
        "conforming_patch_edges": len(patch_edges),
        "support_radius_m": {"reachable": 125, "slow_single": 50, "slow_corroborated": 75},
        "prior_only_vertices": sum(s == 0 for s in support),
        "bounded_vertices": sum(math.isfinite(hi) for _, hi in bounds),
        "disconnected_smoothing_edges": 0,
        "regularization": "area_integrated_linear_gradient",
        "prior_integral_weight": round(sum(mass), 6),
        "solver_iteration_limit": 3000,
        "weakened_smoothing_edges": weakened,
        "protected_routes": len(verified),
        "hard_constraint_routes": len(protected),
        "borderline_routes": sum(
            abs(o.seconds - target) <= BOUNDARY_TOLERANCE_S for o, _ in verified
        ),
        "threshold_constraint_routes": {
            f"{t:g}": sum(abs(o.seconds - t) > BOUNDARY_TOLERANCE_S for o, _ in verified)
            for t in thresholds
        },
        "boundary_tolerance_s": BOUNDARY_TOLERANCE_S,
        "constraint_conflicts": len(conflicts) + data_conflicts + len(response_conflicts),
        "route_response_conflicts": len(response_conflicts),
        "single_slow_hard_floors": sum(
            o.seconds > target + BOUNDARY_TOLERANCE_S for o, _ in verified
        ),
        "rollback_vertices": len(rollback),
        "path_constraints": len(constraints),
        "path_violations": sum(v > 1e-5 for v in path_violations),
        "max_path_violation_s": round(max(path_violations, default=0), 6),
        "invalid_path_segments": invalid_segments,
        "clipped_paths": clipped_paths,
        "water_path_conflicts": water_path_conflicts,
        "boundary_clipped": edge_reachable,
        "ready": not (
            conflicts
            or data_conflicts
            or response_conflicts
            or invalid_segments
            or clipped_paths
            or edge_reachable
        )
        and max(path_violations, default=0) <= 1e-5
        and change < 0.025,
    }
    return field, triangles, quality


def hole_evidence(
    rings,
    observations,
    target,
    routes=(),
    stable_rings=None,
    *,
    clues=(),
    sensitivity_rings=None,
    domain=None,
):
    """内部排除区需两个分散的超时路线点，且至少三个方向有周边可达证据。

    这只是一道证据门槛，不表示路线点证明了整个孔洞都不可达。
    没有证据时拒绝发布该候选，不靠删除内环掩盖耗时场的问题。
    """
    holes = [ring for ring in rings if ring_area(ring) < 0]
    strong = [o for o in observations if o.protected and o.seconds > target + BOUNDARY_TOLERANCE_S]
    counts = []
    surrounded = []
    crossings = []
    stable = []
    records = []
    if domain is not None:
        from shapely.geometry import Polygon

        water = domain.xy_shape(domain.water)
        # 计算网格的米坐标保留6位小数，沿水边的环最多有微米级舍入差。
        # 仅吸收这层数值误差，不以平方米比例容差吞掉真正的陆地缺口。
        rounded_water = water.buffer(1e-6)
    for ring in holes:
        if domain is not None:
            hole = Polygon(ring)
            if hole.difference(water).area <= 1e-6 or rounded_water.covers(hole):
                counts.append(0)
                surrounded.append(0)
                crossings.append(0)
                stable.append(True)
                records.append(
                    {
                        "id": len(records),
                        "ring_xy": ring,
                        "area_m2": abs(ring_area(ring)),
                        "supported": True,
                        "status": "geographic",
                        "source": domain.source,
                        "slow_points": 0,
                        "surrounding_sectors": [],
                        "path_crossings": 0,
                        "fast_inside": False,
                        "mesh_stable": True,
                        "support_stable": True,
                        "spatial_coverage": True,
                        "clue_ids": [],
                        "confirmed_clue_ids": [],
                    }
                )
                continue
        selected = []
        for o in strong:
            if inside_ring(o.x, o.y, ring) and all(
                math.hypot(o.x - q.x, o.y - q.y) >= 25 for q in selected
            ):
                selected.append(o)
        counts.append(len(selected))
        crossings.append(
            sum(
                route.seconds <= target
                and any(
                    segment_crosses_ring(a, b, ring)
                    for a, b in zip(route.path, route.path[1:], strict=False)
                )
                for route in routes
            )
        )
        from .holepolicy import associated_clues, shape_stable, topology_stable

        stable.append(
            stable_rings is not None
            and topology_stable(rings, stable_rings)
            and any(
                ring_area(q) < 0
                and len(selected) >= 2
                and all(inside_ring(o.x, o.y, q) for o in selected)
                for q in stable_rings
            )
            and shape_stable(ring, stable_rings)
        )
        cx = sum(p[0] for p in ring) / len(ring)
        cy = sum(p[1] for p in ring) / len(ring)
        sectors = set()
        for o in observations:
            if not o.protected or o.seconds >= target - BOUNDARY_TOLERANCE_S:
                continue
            if inside_ring(o.x, o.y, ring):
                continue
            if min(math.hypot(o.x - x, o.y - y) for x, y in ring) <= 200:
                sectors.add(int((math.atan2(o.y - cy, o.x - cx) % math.tau) / (math.pi / 2)))
        surrounded.append(len(sectors))
        linked = associated_clues(ring, clues)
        sensitivity = sensitivity_rings is None or all(
            topology_stable(rings, variant) and shape_stable(ring, variant)
            for variant in sensitivity_rings
        )
        # 大孔洞不能只凭两个相邻慢点：每个边界采样顶点须在慢点支持范围内。
        spatial_coverage = bool(selected) and all(
            min(math.dist(p, (o.x, o.y)) for o in selected) <= 100 for p in ring
        )
        fast_inside = any(
            o.protected and o.seconds <= target and inside_ring(o.x, o.y, ring)
            for o in observations
        )
        supported = (
            len(selected) >= 2
            and len(sectors) >= 3
            and not crossings[-1]
            and stable[-1]
            and sensitivity
            and spatial_coverage
            and not fast_inside
        )
        records.append(
            {
                "id": len(records),
                "ring_xy": ring,
                "area_m2": abs(ring_area(ring)),
                "supported": supported,
                "slow_points": len(selected),
                "surrounding_sectors": sorted(sectors),
                "path_crossings": crossings[-1],
                "fast_inside": fast_inside,
                "mesh_stable": stable[-1],
                "support_stable": sensitivity,
                "spatial_coverage": spatial_coverage,
                "clue_ids": [c["id"] for c in linked],
                "confirmed_clue_ids": [c["id"] for c in linked if c["status"] == "confirmed"],
                "status": "supported"
                if supported
                else "conflict"
                if fast_inside or crossings[-1]
                else "insufficient",
            }
        )
    supported = [r["supported"] for r in records]
    return {
        "holes": len(holes),
        "supported_holes": sum(supported),
        "unsupported_holes": sum(not ok for ok in supported),
        "hole_evidence_points": counts,
        "hole_surrounding_sectors": surrounded,
        "hole_path_crossings": crossings,
        "hole_stable": stable,
        "hole_records": records,
    }


def contour_field(vertices, triangles, target):
    fragments = []
    for indices in triangles:
        part = _clip_time(tuple(vertices[i] for i in indices), target)
        if len(part) >= 3 and abs(ring_area(part)) > 1e-6:
            fragments.append(part)
    rings = merge_fragments(fragments)

    # 只去掉同一直线上的冗余点，禁止视觉圆角修改测量边界。
    def compact(ring):
        return [
            b
            for a, b, c in zip([ring[-1], *ring[:-1]], ring, [*ring[1:], ring[0]], strict=True)
            if abs((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])) > 1e-5
        ]

    rings = [compact(r) for r in rings]
    return MeshContour(
        rings,
        [],
        sum(abs(ring_area(p)) for p in fragments),
        0,
        len(triangles),
        0,
        fragments,
        [],
        triangles,
        vertices,
    )
