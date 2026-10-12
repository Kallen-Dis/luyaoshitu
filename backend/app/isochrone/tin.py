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
from collections import defaultdict
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
