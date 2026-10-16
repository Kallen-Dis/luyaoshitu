"""三角网上的受约束局部线性插值：网格交叉校核用它把散点测时连成「校核估计」范围。

做法与 SciPy ``griddata(method="linear")`` 同一原理：先把散点剖分成 Delaunay 三角形，
再在每个三角形里按重心坐标线性插值，凸包以外不外推。在此之上加两条约束：

1. **不跨绕行线索插值。** 三角形的一条边若满足 |D(a) − D(b)| > κ·s + 2ε
   （D 为中心到该点的路网距离，s 为两点直线距离），说明两点之间要绕远，
   直接线性插值会把障碍抹平。这时三角形内每一点只在「与离它最近的顶点连续相通」的顶点之间插值，
   分界落在两侧顶点的中垂线附近。κ、ε 都是经验参数，不是已证明的误差上限。
2. **未知就是未知。** 有测距失败的顶点、或有边长超过 max_edge_m（跨过没测过的空档）的三角形，
   整块记为未知：既不算可达，也不挖掉。

面积按 raster_m 米栅格积分，边界由栅格上的 marching squares 求出。纯 Python 实现，
后端不为此引入 NumPy / SciPy；几百个点、10 米栅格约在一秒以内。
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field

INF = math.inf


@dataclass(frozen=True)
class Vertex:
    x: float  # 以中心为原点的东向米
    y: float  # 北向米
    t: float | None  # 有效耗时（秒）；None = 未知；inf = 受阻走不到
    d: float | None  # 中心到该点的路网距离（米），判绕行线索用；None 表示没有


def discontinuous(a: Vertex, b: Vertex, kappa: float, eps_m: float) -> bool:
    """两点之间是否有绕行线索：按三角不等式，b 的路网距离不可能比 a 多出 κ·s + 2ε 还多，
    除非两点之间要绕远（或至少一端被接口吸附到了别的路上）。受阻点与任何点都不连续。"""
    if a.t is None or b.t is None:
        return False
    if math.isinf(a.t) != math.isinf(b.t):
        return True
    if a.d is None or b.d is None:
        return False
    s = math.hypot(a.x - b.x, a.y - b.y)
    return abs(a.d - b.d) > kappa * s + 2 * eps_m


# ---------- Delaunay 三角剖分（Bowyer–Watson） ----------


def _jittered(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """规则格网大量四点共圆，按序号加毫米级的确定性扰动，剖分结果唯一且可复现。"""
    out = []
    for i, (x, y) in enumerate(points):
        h = (i * 2654435761) % 1000003
        out.append((x + (h % 1000) * 1e-6, y + (h // 1000 % 1000) * 1e-6))
    return out


def _circle(p, a, b, c) -> tuple[float, float, float]:
    ax, ay = p[a]
    bx, by = p[b]
    cx, cy = p[c]
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return 0.0, 0.0, INF
    a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / d
    uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / d
    return ux, uy, (ax - ux) ** 2 + (ay - uy) ** 2


def delaunay(points: list[tuple[float, float]]) -> list[tuple[int, int, int]]:
    """返回三角形（顶点下标，逆时针）。点数少于 3 返回空表。重复点需调用方先去掉。"""
    n = len(points)
    if n < 3:
        return []
    p = _jittered(points)
    xs = [q[0] for q in p]
    ys = [q[1] for q in p]
    span = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
    mx, my = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
    # 超级三角形要足够大，否则凸包边上的细长三角形会连到它的顶点上、最后被一起删掉
    big = 1000 * span
    p += [(mx - 2 * big, my - big), (mx + 2 * big, my - big), (mx, my + 2 * big)]

    def make(a, b, c):
        # 统一成逆时针，边的方向才能在插入时可靠地抵消
        (ax, ay), (bx, by), (cx, cy) = p[a], p[b], p[c]
        if (bx - ax) * (cy - ay) - (by - ay) * (cx - ax) < 0:
            b, c = c, b
        return (a, b, c, *_circle(p, a, b, c))

    tris = [make(n, n + 1, n + 2)]
    for i in sorted(range(n), key=lambda k: (p[k][0], p[k][1])):
        x, y = p[i]
        bad, keep = [], []
        for t in tris:
            (bad if (x - t[3]) ** 2 + (y - t[4]) ** 2 < t[5] else keep).append(t)
        edges: dict[tuple[int, int], int] = {}
        for t in bad:
            for e in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
                if (e[1], e[0]) in edges:
                    del edges[(e[1], e[0])]
                else:
                    edges[e] = 1
        tris = keep + [make(a, b, i) for a, b in edges]
    return [(t[0], t[1], t[2]) for t in tris if t[0] < n and t[1] < n and t[2] < n]


# ---------- 插值到栅格 ----------

OUTSIDE = None  # 凸包以外
UNKNOWN = math.nan  # 凸包以内、但所在三角形是未知块


@dataclass
class Raster:
    x0: float  # 第 0 列像素中心的东向米
    y0: float  # 第 0 行像素中心的北向米
    res: float
    nx: int
    ny: int
    values: list[list[float | None]] = field(default_factory=list)  # [行][列]
    barrier_triangles: int = 0
    unknown_triangles: int = 0
    triangles: int = 0

    def center(self, i: int, j: int) -> tuple[float, float]:
        return self.x0 + j * self.res, self.y0 + i * self.res


def interpolate(
    vertices: list[Vertex],
    res_m: float = 10.0,
    kappa: float = 2.0,
    eps_m: float = 30.0,
    max_edge_m: float = 300.0,
) -> Raster:
    pts = [(v.x, v.y) for v in vertices]
    tris = delaunay(pts)
    xs = [v.x for v in vertices] or [0.0]
    ys = [v.y for v in vertices] or [0.0]
    # 像素中心错开半格：顶点常落在整十米上，像素中心若正好压在三角形边上，凸包边界会被多算一圈
    x0 = math.floor(min(xs) / res_m) * res_m - res_m / 2
    y0 = math.floor(min(ys) / res_m) * res_m - res_m / 2
    nx = int(math.ceil((max(xs) - x0) / res_m)) + 2
    ny = int(math.ceil((max(ys) - y0) / res_m)) + 2
    values: list[list[float | None]] = [[OUTSIDE] * nx for _ in range(ny)]
    raster = Raster(x0, y0, res_m, nx, ny, values, triangles=len(tris))

    for a, b, c in tris:
        va, vb, vc = vertices[a], vertices[b], vertices[c]
        tri = (va, vb, vc)
        long_edge = any(
            math.hypot(u.x - w.x, u.y - w.y) > max_edge_m for u, w in ((va, vb), (vb, vc), (vc, va))
        )
        unknown = long_edge or any(v.t is None for v in tri)
        if unknown:
            raster.unknown_triangles += 1
        cont = {
            (i, j): not discontinuous(tri[i], tri[j], kappa, eps_m)
            for i, j in ((0, 1), (1, 2), (0, 2))
        }
        broken = not unknown and not all(cont.values())
        if broken:
            raster.barrier_triangles += 1

        det = (vb.y - vc.y) * (va.x - vc.x) + (vc.x - vb.x) * (va.y - vc.y)
        if abs(det) < 1e-9:
            continue
        jmin = max(0, int(math.floor((min(va.x, vb.x, vc.x) - x0) / res_m)))
        jmax = min(nx - 1, int(math.ceil((max(va.x, vb.x, vc.x) - x0) / res_m)))
        imin = max(0, int(math.floor((min(va.y, vb.y, vc.y) - y0) / res_m)))
        imax = min(ny - 1, int(math.ceil((max(va.y, vb.y, vc.y) - y0) / res_m)))
        for i in range(imin, imax + 1):
            py = y0 + i * res_m
            row = values[i]
            for j in range(jmin, jmax + 1):
                if row[j] is not OUTSIDE:
                    continue  # 共享边上的像素只认先到的三角形
                px = x0 + j * res_m
                l1 = ((vb.y - vc.y) * (px - vc.x) + (vc.x - vb.x) * (py - vc.y)) / det
                l2 = ((vc.y - va.y) * (px - vc.x) + (va.x - vc.x) * (py - vc.y)) / det
                l3 = 1.0 - l1 - l2
                if l1 < -1e-9 or l2 < -1e-9 or l3 < -1e-9:
                    continue
                if unknown:
                    row[j] = UNKNOWN
                    continue
                lam = (l1, l2, l3)
                if not broken:
                    row[j] = _blend(tri, lam, (0, 1, 2))
                    continue
                near = min(range(3), key=lambda k: (tri[k].x - px) ** 2 + (tri[k].y - py) ** 2)
                row[j] = _blend(tri, lam, _side(near, cont))
    return raster


def _side(k: int, cont: dict[tuple[int, int], bool]) -> tuple[int, ...]:
    """三角形里与顶点 k 经连续边相通的顶点（含 k 自己）。"""
    linked = {k}
    for _ in range(2):
        for (i, j), ok in cont.items():
            if ok and (i in linked or j in linked):
                linked |= {i, j}
    return tuple(sorted(linked))


def _blend(tri: tuple[Vertex, Vertex, Vertex], lam: tuple[float, ...], side: tuple[int, ...]):
    if any(math.isinf(tri[k].t) for k in side):  # type: ignore[arg-type]
        return INF
    w = sum(lam[k] for k in side)
    if w <= 1e-12:
        return float(tri[side[0]].t)  # type: ignore[arg-type]
    return sum(lam[k] * tri[k].t for k in side) / w  # type: ignore[operator]


# ---------- 面积与等值线 ----------


def _reachable(v: float | None, target: float) -> bool:
    return v is not None and not math.isnan(v) and v <= target


def areas_m2(raster: Raster, target: float) -> tuple[float, float]:
    """(可达面积, 未知面积)，平方米。"""
    cell = raster.res * raster.res
    ok = unknown = 0
    for row in raster.values:
        for v in row:
            if v is None:
                continue
            if math.isnan(v):
                unknown += 1
            elif v <= target:
                ok += 1
    return ok * cell, unknown * cell


def contour_rings(raster: Raster, target: float) -> list[list[tuple[float, float]]]:
    """可达区（值 ≤ target）的边界环，局部米制坐标。用 marching squares，交点按线性插值。

    未知、凸包外和受阻都当作不可达处理，所以环总是闭合的。交点用符号键（所在像素边）
    标识，相邻方格算出的同一点必然相同，不靠浮点坐标比较。
    """
    ny, nx, res = raster.ny, raster.nx, raster.res
    big = target + 10 * 3600.0

    def val(i: int, j: int) -> float:
        if i < 0 or j < 0 or i >= ny or j >= nx:
            return big
        v = raster.values[i][j]
        if v is None or math.isnan(v) or math.isinf(v):
            return big
        return v

    def point(key) -> tuple[float, float]:
        kind, i, j = key
        if kind == "h":  # (i, j) 与 (i, j+1) 之间
            a, b = val(i, j), val(i, j + 1)
            f = 0.5 if a == b else min(1.0, max(0.0, (target - a) / (b - a)))
            return raster.x0 + (j + f) * res, raster.y0 + i * res
        a, b = val(i, j), val(i + 1, j)  # "v"：(i, j) 与 (i+1, j) 之间
        f = 0.5 if a == b else min(1.0, max(0.0, (target - a) / (b - a)))
        return raster.x0 + j * res, raster.y0 + (i + f) * res

    links: dict[tuple, list[tuple]] = defaultdict(list)
    for i in range(-1, ny):
        for j in range(-1, nx):
            # 方格四角：左下 (i,j)、右下 (i,j+1)、右上 (i+1,j+1)、左上 (i+1,j)
            bl, br = val(i, j) <= target, val(i, j + 1) <= target
            tr, tl = val(i + 1, j + 1) <= target, val(i + 1, j) <= target
            code = bl | (br << 1) | (tr << 2) | (tl << 3)
            if code in (0, 15):
                continue
            bottom, right = ("h", i, j), ("v", i, j + 1)
            top, left = ("h", i + 1, j), ("v", i, j)
            if code in (5, 10):
                # 鞍点：四角均值决定对角的两个可达角是否经方格中心相通
                mid = (val(i, j) + val(i, j + 1) + val(i + 1, j + 1) + val(i + 1, j)) / 4
                joined = mid <= target
                if code == 5:  # 左下、右上可达
                    pairs = (
                        [(left, top), (bottom, right)] if joined else [(left, bottom), (top, right)]
                    )
                else:  # 右下、左上可达
                    pairs = (
                        [(left, bottom), (top, right)] if joined else [(left, top), (bottom, right)]
                    )
            else:
                sides = ((bottom, bl, br), (right, br, tr), (top, tr, tl), (left, tl, bl))
                crossing = [e for e, a, b in sides if a != b]
                pairs = [(crossing[0], crossing[1])]
            for a, b in pairs:
                links[a].append(b)
                links[b].append(a)

    rings: list[list[tuple[float, float]]] = []
    seen: set[tuple] = set()
    for start in sorted(links):
        if start in seen:
            continue
        ring = [start]
        seen.add(start)
        prev, cur = None, start
        while True:
            nxt = next((k for k in links[cur] if k != prev and k not in seen), None)
            if nxt is None:
                break
            ring.append(nxt)
            seen.add(nxt)
            prev, cur = cur, nxt
        if len(ring) >= 3:
            rings.append([point(k) for k in ring])
    return rings


def ring_area(ring: list[tuple[float, float]]) -> float:
    """有向面积（逆时针为正）。"""
    s = 0.0
    for k in range(len(ring)):
        x1, y1 = ring[k]
        x2, y2 = ring[(k + 1) % len(ring)]
        s += x1 * y2 - x2 * y1
    return s / 2


def inside_ring(x: float, y: float, ring: list[tuple[float, float]]) -> bool:
    inside = False
    for k in range(len(ring)):
        x1, y1 = ring[k]
        x2, y2 = ring[(k + 1) % len(ring)]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def classify_rings(rings: list[list[tuple[float, float]]]) -> list[bool]:
    """每个环是不是孔洞：被奇数个别的环包住的是孔洞（奇偶规则）。"""
    holes = []
    for k, ring in enumerate(rings):
        x, y = ring[0]
        depth = sum(1 for m, other in enumerate(rings) if m != k and inside_ring(x, y, other))
        holes.append(depth % 2 == 1)
    return holes


@dataclass
class MeshContour:
    """直接裁三角形得到的面；未知保持单独几何，不伪装成超时证据。"""

    rings: list[list[tuple[float, float]]]
    unknown_rings: list[list[tuple[float, float]]]
    area_m2: float
    unknown_m2: float
    triangles: int
    barrier_triangles: int
    fragments: list[list[tuple[float, float]]]
    unknown_fragments: list[list[tuple[float, float]]]
    valid_triangles: list[tuple[int, int, int]]
    field_vertices: list[Vertex]


def _clip_time(tri: tuple[Vertex, Vertex, Vertex], target: float) -> list[tuple[float, float]]:
    out = []
    previous = tri[-1]
    for current in tri:
        before = float(previous.t) <= target  # 调用方先排除未知及无穷值
        after = float(current.t) <= target
        if before != after:
            ratio = (target - float(previous.t)) / (float(current.t) - float(previous.t))
            out.append(
                (
                    previous.x + ratio * (current.x - previous.x),
                    previous.y + ratio * (current.y - previous.y),
                )
            )
        if after:
            out.append((current.x, current.y))
        previous = current
    return out


def merge_fragments(fragments: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    """无重叠三角片共享边抵消，再追踪外环/孔洞。仅量化到微米防浮点分叉。"""
    # 同一交点从共享边两侧计算，可能恰落在取整分界两侧，相差 1 微米。
    # 合并量化误差范围内的代表点；不把真实的米级缺口强行连接起来。
    canonical = {}
    micro = defaultdict(list)
    for p in sorted({(round(x, 6), round(y, 6)) for f in fragments for x, y in f}):
        cell = (math.floor(p[0] / 2e-6), math.floor(p[1] / 2e-6))
        close = [
            q
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            for q in micro.get((cell[0] + dx, cell[1] + dy), ())
            if math.dist(p, q) <= 1.5e-6
        ]
        canonical[p] = min(close) if close else p
        if not close:
            micro[cell].append(p)
    points_by_bucket = defaultdict(set)
    for point in canonical.values():
        points_by_bucket[(math.floor(point[0] / 50), math.floor(point[1] / 50))].add(point)

    def split_edge(a, b):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length2 = dx * dx + dy * dy
        if length2 < 1e-12:
            return []
        stops = {a: 0.0, b: 1.0}
        for bx in range(math.floor(min(a[0], b[0]) / 50), math.floor(max(a[0], b[0]) / 50) + 1):
            for by in range(math.floor(min(a[1], b[1]) / 50), math.floor(max(a[1], b[1]) / 50) + 1):
                for p in points_by_bucket.get((bx, by), ()):
                    fraction = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2
                    if not 0 < fraction < 1:
                        continue
                    error = abs((p[0] - a[0]) * dy - (p[1] - a[1]) * dx) / math.sqrt(length2)
                    if error <= 2e-6:
                        stops[p] = fraction
        points = sorted(stops, key=lambda p: stops[p])
        return list(zip(points, points[1:], strict=False))

    # 每个裁片贡献闭合有向环。微米级薄片会让分边后出现同向重复边，
    # set 丢掉一次贡献会破坏入度/出度平衡。保留重数、逐次抵消反向边，
    # 不扩大吸附容差，也不在相隔数米的断点之间补线。
    edges: Counter = Counter()
    for fragment in fragments:
        points = [canonical[(round(x, 6), round(y, 6))] for x, y in fragment]
        if len(points) < 3 or abs(ring_area(points)) < 1e-6:
            continue
        if ring_area(points) < 0:
            points.reverse()
        for a, b in zip(points, [*points[1:], points[0]], strict=True):
            if a == b:
                continue
            for u, v in split_edge(a, b):
                reverse = (v, u)
                if edges[reverse]:
                    edges[reverse] -= 1
                    if not edges[reverse]:
                        del edges[reverse]
                else:
                    edges[(u, v)] += 1

    outgoing: dict[tuple[float, float], set[tuple[float, float]]] = defaultdict(set)
    for a, b in edges:
        outgoing[a].add(b)
    rings = []
    while edges:
        start, current = min(edges)
        edges[(start, current)] -= 1
        if not edges[(start, current)]:
            del edges[(start, current)]
            outgoing[start].remove(current)
        ring = [start]
        previous = start
        while current != start:
            ring.append(current)
            candidates = outgoing[current]
            if not candidates:
                raise ValueError("三角网边界不闭合，不能输出融合圈")
            dx, dy = current[0] - previous[0], current[1] - previous[1]

            def turn(point, current=current, dx=dx, dy=dy):
                ux, uy = point[0] - current[0], point[1] - current[1]
                return (math.atan2(dx * uy - dy * ux, dx * ux + dy * uy) % (2 * math.pi), point)

            nxt = min(candidates, key=turn)
            edges[(current, nxt)] -= 1
            if not edges[(current, nxt)]:
                del edges[(current, nxt)]
                outgoing[current].remove(nxt)
            previous, current = current, nxt
        if len(ring) >= 3 and abs(ring_area(ring)) >= 1e-6:
            rings.append(ring)
    return sorted(rings, key=lambda ring: -abs(ring_area(ring)))


def _nearest_sectors(tri):
    """强跳变片按最近观测点分侧，不借第三点把不连续边重新连通。位置只是模型估计。"""
    for owner in tri:
        polygon = [(v.x, v.y) for v in tri]
        for other in tri:
            if other is owner:
                continue
            nx, ny = 2 * (other.x - owner.x), 2 * (other.y - owner.y)
            bound = other.x**2 + other.y**2 - owner.x**2 - owner.y**2
            out = []
            previous = polygon[-1]
            pv = bound - nx * previous[0] - ny * previous[1]
            for current in polygon:
                cv = bound - nx * current[0] - ny * current[1]
                if (pv >= 0) != (cv >= 0):
                    fraction = pv / (pv - cv)
                    out.append(
                        (
                            previous[0] + fraction * (current[0] - previous[0]),
                            previous[1] + fraction * (current[1] - previous[1]),
                        )
                    )
                if cv >= 0:
                    out.append(current)
                previous, pv = current, cv
            polygon = out
            if not polygon:
                break
        if len(polygon) >= 3 and abs(ring_area(polygon)) > 1e-6:
            yield owner, polygon


def clip_mesh(mesh: MeshContour, target: float) -> MeshContour:
    """从同一个耗时场提取内圈，不重复剖分，也不改变强跳变的分侧。"""
    fragments = []
    for indices in mesh.valid_triangles:
        fragment = _clip_time(tuple(mesh.field_vertices[i] for i in indices), target)
        if len(fragment) >= 3 and abs(ring_area(fragment)) > 1e-6:
            fragments.append(fragment)
    return MeshContour(
        rings=merge_fragments(fragments),
        unknown_rings=mesh.unknown_rings,
        area_m2=sum(abs(ring_area(p)) for p in fragments),
        unknown_m2=mesh.unknown_m2,
        triangles=mesh.triangles,
        barrier_triangles=mesh.barrier_triangles,
        fragments=fragments,
        unknown_fragments=mesh.unknown_fragments,
        valid_triangles=mesh.valid_triangles,
        field_vertices=mesh.field_vertices,
    )


def contour_mesh(
    vertices: list[Vertex],
    target: float,
    kappa: float = 2.0,
    eps_m: float = 30.0,
    max_edge_m: float = 300.0,
    *,
    guard_target: float | None = None,
    resolve_barriers: bool = False,
) -> MeshContour:
    """按线性耗时裁片；融合模式的强跳变分侧估计，长边与未知顶点单列未知。

    距离跳变是两点之间绕行的线索，不否定中心到两点各自的测时。
    只有顶点分处主阈值两侧时，强跳变才阻止边界插值，不能借第三顶点绕过。
    内圈传入同一 guard_target，和主圈、热力共用有效三角片，避免口径不同。
    resolve_barriers 时按最近观测点分侧，不跨强跳变混合；clip_mesh 复用该耗时场。
    边界位置是采样间的估计，不把几何分辨率称作实测精度。
    """
    guard_target = target if guard_target is None else guard_target
    triangles = delaunay([(v.x, v.y) for v in vertices])
    reachable = []
    unknown = []
    barriers = 0
    valid = []
    field_vertices = list(vertices)
    for a, b, c in triangles:
        tri = (vertices[a], vertices[b], vertices[c])
        long = any(
            math.hypot(u.x - v.x, u.y - v.y) > max_edge_m
            for u, v in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0]))
        )
        invalid = any(v.t is None or not math.isfinite(v.t) for v in tri)
        crosses_threshold = not invalid and min(v.t for v in tri) <= guard_target < max(
            v.t for v in tri
        )
        broken = crosses_threshold and any(
            discontinuous(u, v, kappa, eps_m)
            for u, v in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0]))
        )
        if broken:
            barriers += 1
        if long or invalid or (broken and not resolve_barriers):
            unknown.append([(v.x, v.y) for v in tri])
            continue
        if broken:
            for owner, sector in _nearest_sectors(tri):
                indices = []
                for x, y in sector:
                    indices.append(len(field_vertices))
                    field_vertices.append(Vertex(x, y, owner.t, owner.d))
                valid.extend(
                    (indices[0], indices[i], indices[i + 1]) for i in range(1, len(indices) - 1)
                )
                if owner.t <= target:
                    reachable.append(sector)
            continue
        valid.append((a, b, c))
        fragment = _clip_time(tri, target)
        if len(fragment) >= 3 and abs(ring_area(fragment)) >= 1e-6:
            reachable.append(fragment)
    return MeshContour(
        rings=merge_fragments(reachable),
        unknown_rings=merge_fragments(unknown),
        area_m2=sum(abs(ring_area(p)) for p in reachable),
        unknown_m2=sum(abs(ring_area(p)) for p in unknown),
        triangles=len(triangles),
        barrier_triangles=barriers,
        fragments=reachable,
        unknown_fragments=unknown,
        valid_triangles=valid,
        field_vertices=field_vertices,
    )
