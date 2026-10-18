"""融合留出点的空间覆盖计划。只读取冻结的候选场，核验结果不参与选点或拟合。"""

from __future__ import annotations

import math

from .budget import FusionBudgetClient
from .geometry import METERS_PER_DEG_LAT
from .localfield import RayPrior
from .tin import Vertex, _clip_time, inside_ring, ring_area


def interior_candidates(part, *, subdivisions=0):
    """凸差集内的有限搜索；贴近角、边的内缩点补足只测质心的遗漏。

    不是新的测量或面积细分，不能用搜索未找到点来证明整片都不可验证。
    """
    center = tuple(sum(p[k] for p in part) / len(part) for k in (0, 1))
    yield center
    for a, b in zip(part, (*part[1:], part[0]), strict=True):
        for target in (a, ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)):
            for fraction in (0.5, 0.8, 0.98):
                yield tuple(c + fraction * (t - c) for c, t in zip(center, target, strict=True))
    if subdivisions:
        stack = [(part[0], a, b, subdivisions) for a, b in zip(part[1:], part[2:], strict=False)]
        while stack:
            a, b, c, depth = stack.pop()
            if not depth:
                yield tuple((a[k] + b[k] + c[k]) / 3 for k in (0, 1))
                continue
            triangle = (a, b, c)
            i = max(range(3), key=lambda i: math.dist(triangle[i], triangle[(i + 1) % 3]))
            a, b, c = triangle[i], triangle[(i + 1) % 3], triangle[(i + 2) % 3]
            mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            stack.extend(((a, mid, c, depth - 1), (mid, b, c, depth - 1)))


def select_spaced_targets(candidates, limit):
    """先安排可选位置最少的片区，再补方向和分散点；不放宽端点独立门槛。"""
    selected, covered_regions, covered_sectors = [], set(), set()
    remaining = list(candidates)

    def distance(a, b):
        return math.dist(a["xy"], b["xy"])

    while remaining and len(selected) < limit:
        pools = {}
        for q in remaining:
            if q["region"] is not None and q["region"] not in covered_regions:
                pools.setdefault(q["region"], []).append(q)
        if pools:
            region = min(pools, key=lambda r: (len(pools[r]), r))

            def priority(q, pools=pools, region=region):
                # 避免选走邻区唯一入口；其次少损失邻区的其他候选。
                losses = [
                    sum(distance(q, other) < 10 for other in pool)
                    for r, pool in pools.items()
                    if r != region
                ]
                sizes = [len(pool) for r, pool in pools.items() if r != region]
                return (
                    sum(lost == size for lost, size in zip(losses, sizes, strict=True)),
                    sum(lost / size for lost, size in zip(losses, sizes, strict=True)),
                    q["sector"] in covered_sectors,
                    q["source"] != "walking_path",
                    q["threshold_distance"],
                    q["key"],
                )

            q = min(pools[region], key=priority)
        else:
            q = min(
                remaining,
                key=lambda q: (
                    q["sector"] in covered_sectors,
                    q["threshold_distance"],
                    q["key"],
                ),
            )
        selected.append(q)
        if q["region"] is not None:
            covered_regions.add(q["region"])
        covered_sectors.add(q["sector"])
        remaining = [
            item
            for item in remaining
            if distance(item, q) >= (50 if item["region"] == q["region"] else 10)
        ]
    # 贪心可能选走另一片区的唯一合法位置。有限回溯只调整验证目标，
    # 不降低10米间距、不修改模型；搜索用尽仍如实报告未覆盖。
    pools = {}
    for q in candidates:
        if q["region"] is not None:
            pools.setdefault(q["region"], []).append(q)
    if set(pools) - covered_regions and len(pools) <= min(limit, 128):
        attempts = 0

        def assign(pending, chosen):
            nonlocal attempts
            attempts += 1
            if attempts > 2000:
                return None
            if not pending:
                return chosen
            available = {
                r: [q for q in pools[r] if all(distance(q, p) >= 10 for p in chosen)]
                for r in pending
            }
            r = min(pending, key=lambda r: (len(available[r]), r))
            for q in available[r]:
                found = assign(pending - {r}, [*chosen, q])
                if found is not None:
                    return found
            return None

        repaired = assign(set(pools), [])
        if repaired is not None:
            for q in selected:
                if len(repaired) >= limit:
                    break
                if all(
                    distance(q, p) >= (50 if q["region"] == p["region"] else 10) for p in repaired
                ):
                    repaired.append(q)
            selected = repaired
            covered_regions = {q["region"] for q in selected if q["region"] is not None}
            covered_sectors = {q["sector"] for q in selected}
    return selected, covered_regions, covered_sectors


def convex_difference(fragment, mask):
    """凸片差集，用于在真正改变的范围里选点，不用附近的格心冒充片区。"""
    if len(fragment) < 3:
        return []
    if len(mask) < 3:
        return [fragment]
    if ring_area(mask) < 0:
        mask = list(reversed(mask))
    remaining, pieces = fragment, []
    for a, b in zip(mask, (*mask[1:], mask[0]), strict=True):
        inside, outside = [], []
        if not remaining:
            break
        previous = remaining[-1]
        pv = (b[0] - a[0]) * (previous[1] - a[1]) - (b[1] - a[1]) * (previous[0] - a[0])
        for current in remaining:
            cv = (b[0] - a[0]) * (current[1] - a[1]) - (b[1] - a[1]) * (current[0] - a[0])
            if (pv >= 0) != (cv >= 0):
                t = pv / (pv - cv)
                point = (
                    previous[0] + t * (current[0] - previous[0]),
                    previous[1] + t * (current[1] - previous[1]),
                )
                inside.append(point)
                outside.append(point)
            (inside if cv >= 0 else outside).append(current)
            previous, pv = current, cv
        if len(outside) >= 3 and abs(ring_area(outside)) > 1e-6:
            pieces.append(outside)
        remaining = inside
    return pieces


def distance_to_paths(x, y, paths):
    best = math.inf
    for a, b in paths:
        dx, dy = b[0] - a[0], b[1] - a[1]
        size = dx * dx + dy * dy
        t = max(0, min(1, ((x - a[0]) * dx + (y - a[1]) * dy) / size)) if size else 0
        best = min(best, math.hypot(x - a[0] - t * dx, y - a[1] - t * dy))
    return best


def independence_space_blocked(parts, training, paths):
    """保守证明凸片被独立性排除带覆盖；证不出就返回 False，不冒充可选点。

    圆盘和线段缓冲带均为凸集，三角形顶点同在一带内则整片在带内。
    有限二分仅用于证明多个排除带的联合覆盖，不删片区、不改变验收门槛。
    """

    def covered(triangle, points, segments, depth):
        if any(all(math.dist(v, p) < 10 for v in triangle) for p in points) or any(
            all(distance_to_paths(*v, [segment]) <= 5 for v in triangle) for segment in segments
        ):
            return True
        if not depth:
            return False
        i = max(range(3), key=lambda i: math.dist(triangle[i], triangle[(i + 1) % 3]))
        a, b, c = triangle[i], triangle[(i + 1) % 3], triangle[(i + 2) % 3]
        mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        return covered((a, mid, c), points, segments, depth - 1) and covered(
            (mid, b, c), points, segments, depth - 1
        )

    for part in parts:
        bounds = [(min(p[k] for p in part), max(p[k] for p in part)) for k in (0, 1)]
        points = [
            p
            for p in training
            if all(lo - 10 <= p[k] <= hi + 10 for k, (lo, hi) in enumerate(bounds))
        ]
        segments = [
            (a, b)
            for a, b in paths
            if all(
                max(a[k], b[k]) >= lo - 5 and min(a[k], b[k]) <= hi + 5
                for k, (lo, hi) in enumerate(bounds)
            )
        ]
        if not all(
            covered((part[0], a, b), points, segments, 6)
            for a, b in zip(part[1:], part[2:], strict=False)
        ):
            return False
    return bool(parts)


def walking_segments(chk):
    """只读取拟合阶段取得的路线几何，不使用路线沿途推算耗时或核验结果。"""
    segments = set()
    for p in chk.probes.values():
        if p.kind.startswith("validation") or p.blocked:
            continue
        path = [chk.xy(*point) for point in p.route_path]
        for a, b in zip(path, path[1:], strict=False):
            # 太长的单段可能是缺失几何的直线占位，不能拿来当可步行路径。
            if 1 <= math.hypot(a[0] - b[0], a[1] - b[1]) <= 100:
                segments.add(tuple(sorted((a, b))))
    return sorted(segments)


def project_to_walk(x, y, segments, training, *, part=None):
    choices = []
    for (ax, ay), (bx, by) in segments:
        dx, dy = bx - ax, by - ay
        fraction = max(0, min(1, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
        px, py = ax + fraction * dx, ay + fraction * dy
        distance = math.hypot(px - x, py - y)
        if (
            distance <= 50
            and (part is None or inside_ring(px, py, part))
            and all(math.hypot(px - a, py - b) >= 20 for a, b in training)
        ):
            choices.append((distance, px, py))
    return min(choices)[1:] if choices else None


def known_reachable_segments(chk, fusion):
    """只有总耗时不超过阈值的可信路径才已约束沿途的圈内分类。

    超时路线的上限不能确定中途新终点是否可达；端点与训练样本仍须另行去重。
    """
    return [
        (chk.xy(a[1], a[0]), chk.xy(b[1], b[0]))
        for route in fusion.get("training_paths", [])
        if route["seconds"] <= chk.cfg.target_s
        for a, b in zip(route["path"], route["path"][1:], strict=False)
    ]


def correction_regions(faces, parts):
    """按真实差集共边分组；不让三角形中未改变的范围连接两个修正片区。"""
    # 裁剪的共边/共点接触会返回零面积退化片段，不是需要选点的区域。
    # 沿用裁剪的 1 平方毫米数值容差，不按展示面积删除真实小修正。
    pieces = [
        (face, part)
        for face, group in zip(faces, parts, strict=True)
        for part in group
        if len(part) >= 3 and abs(ring_area(part)) > 1e-6
    ]
    parent = list(range(len(pieces)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def share_edge(p, q):
        for a, b in zip(p, (*p[1:], p[0]), strict=True):
            dx, dy = b[0] - a[0], b[1] - a[1]
            length = math.hypot(dx, dy)
            if length <= 1e-6:
                continue
            for c, d in zip(q, (*q[1:], q[0]), strict=True):
                if (
                    max(abs(dx * (v[1] - a[1]) - dy * (v[0] - a[0])) / length for v in (c, d))
                    > 1e-6
                ):
                    continue
                values = [((v[0] - a[0]) * dx + (v[1] - a[1]) * dy) / length for v in (c, d)]
                if min(length, max(values)) - max(0, min(values)) > 1e-6:
                    return True
        return False

    bins = {}
    for i, (_, part) in enumerate(pieces):
        bounds = [(min(p[k] for p in part), max(p[k] for p in part)) for k in (0, 1)]
        keys = [
            (x, y)
            for x in range(
                math.floor((bounds[0][0] - 1e-6) / 50), math.floor((bounds[0][1] + 1e-6) / 50) + 1
            )
            for y in range(
                math.floor((bounds[1][0] - 1e-6) / 50), math.floor((bounds[1][1] + 1e-6) / 50) + 1
            )
        ]
        nearby = {j for key in keys for j in bins.get(key, [])}
        for j in sorted(nearby):
            if share_edge(part, pieces[j][1]):
                parent[root(i)] = root(j)
        for key in keys:
            bins.setdefault(key, []).append(i)
    groups = {}
    for i, piece in enumerate(pieces):
        groups.setdefault(root(i), []).append(piece)
    region_faces = [sorted({tuple(face) for face, _ in group}) for group in groups.values()]
    region_parts = [[part for _, part in group] for group in groups.values()]
    regions = [sorted({i for face in group for i in face}) for group in region_faces]
    return regions, region_faces, region_parts


def validation_plan(chk, lattice, leaves, fusion, ray_samples):
    field = fusion["time_field"]
    vertices = field["vertices"]
    # 新结果保留计算坐标，避免序列化经纬度的毫米级取整制造虚假差集。
    xy = field.get("vertices_xy") or [chk.xy(v[1], v[0]) for v in vertices]
    ray = [chk.xy(lat, lng) for lat, lng in chk.polygon]
    samples = [(*chk.xy(a, b), t, d) for a, b, t, d in ray_samples]
    prior = RayPrior(ray, samples, chk.cfg.target_s)
    from .fusion import _clip_convex

    # 检查三角形内的范围差异，不要求节点修正超过 30 秒。微小阈值跨越也可能改边界。
    changed_faces = []
    changed_parts = []
    for face in field["triangles"]:
        current = _clip_time(
            tuple(Vertex(*xy[i], vertices[i][2], None) for i in face), chk.cfg.target_s
        )
        originals = [
            _clip_convex([xy[i] for i in face], [(0, 0), a, b])
            for a, b in zip(ray, (*ray[1:], ray[0]), strict=True)
        ]
        originals = [p for p in originals if len(p) >= 3 and abs(ring_area(p)) > 1e-6]
        original_area = sum(abs(ring_area(p)) for p in originals)
        overlap = sum(
            abs(ring_area(_clip_convex(current, p)))
            for p in originals
            if len(current) >= 3 and len(p) >= 3
        )
        delta = abs(ring_area(current)) + original_area - 2 * overlap
        # 计算节点的 key 量化到微米；沿三角形周长传播的该级误差不是新片区。
        perimeter = sum(
            math.dist(xy[a], xy[b]) for a, b in zip(face, (*face[1:], face[0]), strict=True)
        )
        if delta > max(1e-6, perimeter * 2e-6):
            changed_faces.append(face)
            parts = [current] if len(current) >= 3 else []
            for original in originals:
                if len(original) >= 3 and abs(ring_area(original)) > 1e-6:
                    parts = [piece for p in parts for piece in convex_difference(p, original)]
            parts += [
                piece
                for original in originals
                if len(original) >= 3
                for piece in convex_difference(original, current)
            ]
            changed_parts.append(parts)
    if chk.cfg.domain is not None:
        from shapely.geometry import Polygon
        from shapely.ops import triangulate

        water = chk.cfg.domain.xy_shape(chk.cfg.domain.water)
        clipped_parts = []
        for group in changed_parts:
            parts = []
            for part in group:
                if len(part) < 3:
                    continue
                remaining = Polygon(part).difference(water)
                parts.extend(
                    list(t.exterior.coords)[:-1]
                    for t in triangulate(remaining)
                    if remaining.covers(t) and t.area > 1e-6
                )
            clipped_parts.append(parts)
        changed_parts = clipped_parts
    regions, region_faces, region_parts = correction_regions(changed_faces, changed_parts)
    training = [
        chk.xy(p["lat"], p["lng"]) for p in fusion["measured_points"] if p["time_s"] is not None
    ]
    # 旧轮次核验结果已经用于决定是否纠错，不再冒充新候选的独立样本。
    training.extend(getattr(chk, "retired_validation_xy", ()))
    candidates = {}
    known_paths = known_reachable_segments(chk, fusion)
    searched = [0] * len(regions)
    rejections = [{"training": 0, "known_path": 0, "region_node": 0} for _ in regions]
    positions = set()

    def add_candidate(key, source, x, y, region, risk_distance=0):
        position = (round(x, 6), round(y, 6), region)
        if position in positions:
            return
        positions.add(position)
        if region is not None:
            searched[region] += 1
        if training and min(math.hypot(x - a, y - b) for a, b in training) < 10:
            if region is not None:
                rejections[region]["training"] += 1
            return
        if distance_to_paths(x, y, known_paths) <= 5:
            if region is not None:
                rejections[region]["known_path"] += 1
            return
        if region is not None and min(math.dist((x, y), xy[i]) for i in regions[region]) > 100:
            rejections[region]["region_node"] += 1
            return
        candidates[key] = {
            "key": key,
            "source": source,
            "lat": chk.center[0] + y / METERS_PER_DEG_LAT,
            "lng": chk.center[1] + x / chk.mpl,
            "xy": (x, y),
            "region": region,
            "sector": int((math.atan2(y, x) % math.tau) / (math.pi / 2)),
            "risk_distance": risk_distance,
            "threshold_distance": abs(prior(x, y) - chk.cfg.target_s),
        }

    # 片区点来自差集内部，不能只在 100 米邻域里找一个看起来方便的点。
    for region, parts in enumerate(region_parts):
        for j, part in enumerate(parts):
            x, y = (sum(p[k] for p in part) / len(part) for k in (0, 1))
            add_candidate((800000 + region, j), "changed_patch", x, y, region)
    for p in leaves:
        if p.kind not in ("cell", "sub", "outer"):
            continue
        for dy, dx in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
            key = (p.key[0] + dy, p.key[1] + dx)
            if key in chk.probes or key in candidates:
                continue
            lat, lng = lattice.at(*key)
            x, y = chk.xy(lat, lng)
            # 直接检查差集归属，不能因为另一片区的三角网节点更近而漏记本片区。
            region = next(
                (
                    r
                    for r, parts in enumerate(region_parts)
                    if any(inside_ring(x, y, p) for p in parts)
                ),
                None,
            )
            add_candidate(key, "grid_offset", x, y, region)

    # 候选不足的片区才扩展内部搜索，不在全图加测或把点挪到片区外。
    initial_counts = [
        sum(q["region"] == r for q in candidates.values()) for r in range(len(regions))
    ]
    slow_segments = sorted(
        {
            tuple(sorted((chk.xy(a[1], a[0]), chk.xy(b[1], b[0]))))
            for route in fusion.get("training_paths", [])
            if route["seconds"] > chk.cfg.target_s
            for a, b in zip(route["path"], route["path"][1:], strict=False)
            if 1 <= math.dist(chk.xy(a[1], a[0]), chk.xy(b[1], b[0])) <= 100
        }
    )
    for region, parts in enumerate(region_parts):
        if initial_counts[region] >= 8:
            continue
        serial = len(parts)
        for part in parts:
            for x, y in interior_candidates(part):
                add_candidate((800000 + region, serial), "changed_patch", x, y, region)
                serial += 1
                # 超时路线仅提供道路位置，不提供中途耗时；新终点仍必须独立测时。
                projected = project_to_walk(x, y, slow_segments, training, part=part)
                if projected is not None:
                    add_candidate((800000 + region, serial), "walking_path", *projected, region)
                    serial += 1
    # 若排除带尚未覆盖整片，进一步细搜内部的小空隙；不能把有限搜索失败当成不可能。
    for region, parts in enumerate(region_parts):
        if any(q["region"] == region for q in candidates.values()):
            continue
        if independence_space_blocked(parts, training, known_paths):
            continue
        serial = 0
        for part in parts:
            for x, y in interior_candidates(part, subdivisions=7):
                add_candidate((810000 + region, serial), "changed_patch", x, y, region)
                serial += 1
    all_candidates = list(candidates.values())
    candidate_counts = [sum(q["region"] == r for q in all_candidates) for r in range(len(regions))]
    # 每个修正片区至少安排一个目标，再留四个方向的余量。点数随片区变化，
    # 请求量仍由预算客户端限制；不能用固定点数上限制造永远无法通过的覆盖计划。
    planned = max(chk.cfg.validation_points, len(regions) + 4)
    selected, covered_regions, covered_sectors = select_spaced_targets(all_candidates, planned)
    for q in selected:
        q["region_nodes"] = (
            [vertices[i][:2] for i in regions[q["region"]]] if q["region"] is not None else []
        )
        q["region_triangles"] = (
            [
                [
                    [
                        chk.center[1] + xy[i][0] / chk.mpl,
                        chk.center[0] + xy[i][1] / METERS_PER_DEG_LAT,
                    ]
                    for i in face
                ]
                for face in region_faces[q["region"]]
            ]
            if q["region"] is not None
            else []
        )
        q["region_polygons"] = (
            [
                [
                    [chk.center[1] + x / chk.mpl, chk.center[0] + y / METERS_PER_DEG_LAT]
                    for x, y in part
                ]
                for part in region_parts[q["region"]]
            ]
            if q["region"] is not None
            else []
        )
    # 在冻结模型和发出请求之前锁定同片区、同方向的一个备选点。
    # 仅在端点不可用时替换；有效的分类分歧不能通过换点丢掉。
    reserved = {q["key"] for q in selected}
    for q in selected:
        alternatives = [
            item
            for item in all_candidates
            if item["key"] not in reserved
            and item["region"] == q["region"]
            and item["sector"] == q["sector"]
            and all(math.dist(item["xy"], other["xy"]) >= 10 for other in selected)
        ]
        alternatives.sort(key=lambda item: (item["source"] != "walking_path", item["key"]))
        q["alternatives"] = alternatives[:3]
        for alternative in q["alternatives"]:
            alternative["region_triangles"] = q["region_triangles"]
            alternative["region_polygons"] = q["region_polygons"]
        reserved.update(item["key"] for item in q["alternatives"])
    unplanned_regions = sorted(set(range(len(regions))) - covered_regions)
    unplanned_sectors = sorted(set(range(4)) - covered_sectors)
    from .holevalidation import add_hole_targets

    hole_tasks, unplanned_hole_tasks = add_hole_targets(chk, fusion, selected)
    # 预算有限时先取得分散方向的证据，再遍历剩余细片区。计划本身不删减，
    # 未执行片区仍计为未完成，不能用四方向核验冒充完整覆盖。
    first = []
    for sector in range(4):
        pool = [q for q in selected if q.get("sector") == sector and not q.get("hole_task")]
        if pool:
            first.append(
                min(
                    pool,
                    key=lambda q: (
                        q["source"] != "walking_path",
                        q.get("region") is not None,
                        q.get("threshold_distance", 0),
                        q["key"],
                    ),
                )
            )
    first_keys = {q["key"] for q in first}
    selected = first + [q for q in selected if q["key"] not in first_keys]
    return selected, {
        "hole_tasks": hole_tasks,
        "unplanned_hole_tasks": unplanned_hole_tasks,
        "required_regions": len(regions),
        "required_sectors": 4,
        "target_points": planned + len(hole_tasks),
        "planned_points": len(selected),
        "planned_regions": len(covered_regions),
        "unplanned_regions": unplanned_regions,
        "unplanned_sectors": unplanned_sectors,
        "unplanned_details": [
            {
                "region": r,
                "searched_points": searched[r],
                "eligible_candidates": candidate_counts[r],
                "rejected_training": rejections[r]["training"],
                "rejected_known_path": rejections[r]["known_path"],
                "rejected_region_node": rejections[r]["region_node"],
                "area_m2": sum(abs(ring_area(p)) for p in region_parts[r]),
                "independence_space_blocked": not candidate_counts[r]
                and independence_space_blocked(region_parts[r], training, known_paths),
                "reason": "候选与其他片区选点间距不足"
                if candidate_counts[r]
                else "片区内搜索未找到满足独立距离的候选",
            }
            for r in unplanned_regions
        ],
        "plan_complete": not unplanned_regions
        and not unplanned_sectors
        and not unplanned_hole_tasks,
        "walking_path_points": sum(q["source"] == "walking_path" for q in selected),
        "regions": [
            {
                "id": j,
                "nodes": len(g),
                "node_indices": g,
                "area_m2": sum(abs(ring_area(p)) for p in region_parts[j]),
                "center": [
                    sum(vertices[i][0] for i in g) / len(g),
                    sum(vertices[i][1] for i in g) / len(g),
                ],
            }
            for j, g in enumerate(regions)
        ],
        "basis": "按三角形内范围差异核验修正片区，包含不足 30 秒的阈值跨越；"
        "已有路径约束与新区域验证分开，核验同时覆盖四个方向。工程覆盖检查，不是统计置信度。",
    }


def validation_request_budget(chk, selections):
    """完成主目标计划的最低新请求数；缓存免费，所有嵌套预算和已用重试均扣除。

    不预言重试次数，也不扩大任何请求上限。端点质量不合格时可能还需备选请求。
    """
    cached = getattr(chk.client, "walking_route_positions_cached", None) or getattr(
        chk.client, "walking_route_cached", None
    )
    cached_points = sum(
        bool(cached and cached(chk.center, (q["lat"], q["lng"]), fine_m=10)) for q in selections
    )
    required = len(selections) - cached_points
    alternatives = [q for selection in selections for q in selection.get("alternatives", [])]
    backup_pool = sum(
        not bool(cached and cached(chk.center, (q["lat"], q["lng"]), fine_m=10))
        for q in alternatives
    )
    available = max(0, chk.cfg.max_routes - chk.budget.routes_used)
    layer = chk.client
    while isinstance(layer, FusionBudgetClient):
        available = min(
            available, max(0, layer.max_routes - max(layer.routes_used, layer.route_attempts))
        )
        layer = layer.client
    # 备选目标全部冻结，但共享剩余额度；不要求每个主目标额外预付一次备选请求。
    spare = max(0, available - required)
    retry_reserve = min(4, math.ceil(required * 0.1), spare)
    backup_requests = min(backup_pool, math.ceil(required * 0.2), max(0, spare - retry_reserve))
    planned_requests = required + backup_requests + retry_reserve
    return {
        "cached_points": cached_points,
        "required_requests": required,
        "available_requests": available,
        "backup_requests": backup_requests,
        "backup_pool": backup_pool,
        "retry_reserve": retry_reserve,
        "planned_requests": planned_requests,
        "shortfall": max(0, planned_requests - available),
        "sufficient": planned_requests <= available,
        "basis": "主目标优先，冻结备选共享预留并保留有限重试；所有请求仍受原硬上限约束。",
    }


async def validate_in_stages(selections, evaluate, record_known):
    """先完成所有主目标，再消耗共享备选；有效判定及路径冲突禁止换点覆盖。"""
    results = []
    attempts = []

    def finish():
        for i, result in enumerate(results):
            result["attempts"] = attempts[i]
        return results

    for selection in selections:
        result = await evaluate(selection, selection["region_nodes"])
        results.append(result)
        attempts.append([result.copy()])
        if result["verdict"] != "unknown":
            record_known(result)
        if (
            result.get("significant_conflict", result["verdict"] == "conflict")
            or result["path_upper_conflict"]
        ):
            return finish()
    for i, selection in enumerate(selections):
        result = results[i]
        if result["verdict"] == "unknown" and not result["path_upper_conflict"]:
            for alternative in selection["alternatives"]:
                result = await evaluate(alternative, selection["region_nodes"])
                attempts[i].append(result.copy())
                if result["verdict"] != "unknown":
                    record_known(result)
                if (
                    result.get("significant_conflict", result["verdict"] == "conflict")
                    or result["path_upper_conflict"]
                ):
                    results[i] = result
                    return finish()
                if result["verdict"] != "unknown" or result["path_upper_conflict"]:
                    break
        result["attempts"] = attempts[i]
        results[i] = result
    return results


def validation_coverage(plan, results):
    known = [p for p in results if p["verdict"] != "unknown"]
    covered = {p["region"] for p in known if p.get("region") is not None}
    sectors = {p["sector"] for p in known}
    missing = sorted(set(range(plan["required_regions"])) - covered)
    required_holes = {t["id"] for t in plan.get("hole_tasks", [])}
    covered_holes = {p.get("hole_task_id") for p in known if p["verdict"] == "consistent"}
    missing_holes = sorted(required_holes - covered_holes)
    return {
        **plan,
        "checked_regions": len(covered),
        "missing_regions": missing,
        "checked_sectors": len(sectors),
        "missing_hole_tasks": missing_holes,
        "hole_validation_complete": not missing_holes and not plan.get("unplanned_hole_tasks"),
        "complete": not missing and not missing_holes and len(sectors) >= plan["required_sectors"],
    }
