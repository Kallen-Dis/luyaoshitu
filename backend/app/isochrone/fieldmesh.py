"""连续的局部三角网及线段约束。计算边不代表道路，交叉线不建立通行关系。"""

from __future__ import annotations

import math
from collections import defaultdict


def barycentric(point, a, b, c):
    """输入均为局部米坐标；退化面积容差不能直接用于经纬度。"""
    x, y = point
    det = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
    if abs(det) < 1e-10:
        return None
    u = ((b[1] - c[1]) * (x - c[0]) + (c[0] - b[0]) * (y - c[1])) / det
    v = ((c[1] - a[1]) * (x - c[0]) + (a[0] - c[0]) * (y - c[1])) / det
    return u, v, 1 - u - v


class FieldMesh:
    def __init__(self, extent, spacing):
        self.spacing = spacing
        half = math.ceil(extent / spacing)
        n = 2 * half + 1
        self.limit = half * spacing
        self.points = [
            (-self.limit + j * spacing, -self.limit + i * spacing)
            for i in range(n)
            for j in range(n)
        ]
        self.lookup = {self.key(p): i for i, p in enumerate(self.points)}
        self.faces = []
        self.bins = defaultdict(set)
        for i in range(n - 1):
            for j in range(n - 1):
                k = i * n + j
                self.add((k, k + 1, k + n + 1))
                self.add((k, k + n + 1, k + n))

    @staticmethod
    def key(point):
        return round(point[0], 6), round(point[1], 6)

    def cells(self, points):
        xs, ys = zip(*points, strict=True)
        for i in range(math.floor(min(xs) / self.spacing), math.floor(max(xs) / self.spacing) + 1):
            for j in range(
                math.floor(min(ys) / self.spacing), math.floor(max(ys) / self.spacing) + 1
            ):
                yield i, j

    def add(self, face):
        a, b, c = [self.points[i] for i in face]
        area = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        if abs(area) < 1e-8:
            return
        if area < 0:
            face = face[0], face[2], face[1]
        index = len(self.faces)
        self.faces.append(face)
        for cell in self.cells((a, b, c)):
            self.bins[cell].add(index)

    def candidates(self, points):
        found = set()
        # 数值上贴着格网线的点必须同时查两侧桶；精确重心判定仍在下游。
        xs, ys = zip(*points, strict=True)
        eps = max(1e-6, self.spacing * 1e-8)
        bounds = ((min(xs) - eps, min(ys) - eps), (max(xs) + eps, max(ys) + eps))
        for cell in self.cells(bounds):
            found.update(self.bins.get(cell, ()))
        return sorted(i for i in found if self.faces[i] is not None)

    def stencil(self, point):
        if self.key(point) in self.lookup:
            return [(self.lookup[self.key(point)], 1.0)]
        for i in self.candidates((point,)):
            face = self.faces[i]
            weights = barycentric(point, *(self.points[k] for k in face))
            if weights is not None and min(weights) >= -1e-8:
                positive = [(k, max(0, w)) for k, w in zip(face, weights, strict=True) if w > 1e-10]
                total = sum(w for _, w in positive)
                return [(k, w / total) for k, w in positive]
        return []

    def insert(self, point):
        key = self.key(point)
        if key in self.lookup:
            return self.lookup[key]
        owners = []
        for i in self.candidates((point,)):
            weights = barycentric(point, *(self.points[k] for k in self.faces[i]))
            if weights is not None and min(weights) >= -1e-8:
                owners.append(i)
        if not owners:
            return None
        k = len(self.points)
        self.points.append(point)
        self.lookup[key] = k
        # 位于公共边时，同时切分两侧三角形，不留下悬空节点。
        for i in owners:
            face = self.faces[i]
            self.faces[i] = None
            for a, b in zip(face, (*face[1:], face[0]), strict=True):
                self.add((a, b, k))
        return k

    def line_stencils(self, a, b):
        """线段在每个三角形内的两个端点。线性场最大值必在这两个端点之一。"""
        for i in self.candidates((a, b)):
            face = self.faces[i]
            pa = barycentric(a, *(self.points[k] for k in face))
            pb = barycentric(b, *(self.points[k] for k in face))
            if pa is None or pb is None:
                continue
            low, high = 0.0, 1.0
            for u, v in zip(pa, pb, strict=True):
                delta = v - u
                if abs(delta) < 1e-12:
                    if u < -1e-8:
                        high = -1
                        break
                elif delta > 0:
                    low = max(low, -u / delta)
                else:
                    high = min(high, -u / delta)
            if low > high + 1e-9:
                continue
            for t in (max(0, low), min(1, high)):
                weights = [
                    (k, max(0, u + t * (v - u))) for k, u, v in zip(face, pa, pb, strict=True)
                ]
                total = sum(w for _, w in weights)
                yield [(k, w / total) for k, w in weights if w > 1e-10]

    def conform_segment(self, a, b):
        """把约束线段切成网格边；只加计算节点，不增加实测证据。"""
        self.insert(a)
        self.insert(b)
        crossings = [
            tuple(sum(self.points[k][axis] * w for k, w in weights) for axis in (0, 1))
            for weights in self.line_stencils(a, b)
        ]
        for point in crossings:
            self.insert(point)

    def improve_quality(self, max_passes=24):
        """插点后翻转劣质对角线；必须在加入证据/边界约束边之前调用。

        不移动任何节点、不增加实测、不改变计算域。凸四边形内用局部
        Delaunay条件减少瘦长单元，避免有限元迭代受插点顺序严重影响。
        """

        def cross(a, b, c):
            return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

        flips = 0
        for _ in range(max_passes):
            edges = {}
            for i, face in enumerate(self.faces):
                if face is not None:
                    for a, b, c in (
                        (face[0], face[1], face[2]),
                        (face[1], face[2], face[0]),
                        (face[2], face[0], face[1]),
                    ):
                        edges.setdefault(tuple(sorted((a, b))), []).append((i, c))
            changed = 0
            used = set()
            for (a, b), pairs in sorted(edges.items()):
                if len(pairs) != 2:
                    continue
                (i, c), (j, d) = pairs
                if i in used or j in used:
                    continue
                pa, pb, pc, pd = [self.points[k] for k in (a, b, c, d)]
                if cross(pc, pd, pa) * cross(pc, pd, pb) >= -1e-8:
                    continue

                def cot(p, pa=pa, pb=pb):
                    return sum((pa[k] - p[k]) * (pb[k] - p[k]) for k in (0, 1)) / max(
                        abs(cross(p, pa, pb)), 1e-12
                    )

                if cot(pc) + cot(pd) >= -1e-8:
                    continue
                self.faces[i] = self.faces[j] = None
                used.update((i, j))
                self.add((c, d, a))
                self.add((d, c, b))
                changed += 1
            flips += changed
            if not changed:
                break
        return flips


def segment_crosses_ring(a, b, ring):
    """检查穿过孔洞内部，不能只看路径端点。边界相切不算穿越。"""
    from .tin import inside_ring

    cuts = [0.0, 1.0]
    dx, dy = b[0] - a[0], b[1] - a[1]
    for c, d in zip(ring, (*ring[1:], ring[0]), strict=True):
        ex, ey = d[0] - c[0], d[1] - c[1]
        det = dx * ey - dy * ex
        if abs(det) < 1e-10:
            continue
        cx, cy = c[0] - a[0], c[1] - a[1]
        t, u = (cx * ey - cy * ex) / det, (cx * dy - cy * dx) / det
        if 0 <= t <= 1 and 0 <= u <= 1:
            cuts.append(t)
    cuts = sorted(set(cuts))
    return any(
        inside_ring(a[0] + dx * (u + v) / 2, a[1] + dy * (u + v) / 2, ring)
        for u, v in zip(cuts, cuts[1:], strict=False)
        if v - u > 1e-8
    )
