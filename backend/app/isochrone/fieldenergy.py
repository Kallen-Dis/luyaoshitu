"""按物理面积积分的分片线性残差正则项；节点增加不等于证据增加。"""

from __future__ import annotations

from .holepolicy import smoothing_factor


def regularization(points, triangles, anchors, clues, jump):
    """能量为 0.35/2500 ∫r² + 2∫a|∇r|²；先验项使用集中质量近似。

    2500 平方米是固定的参考面积，不跟计算网格间距一起改变。
    每个三角形的梯度能量非负；钝角产生的正非对角项不能截成零，
    否则会破坏相同线性场在不同网格上的能量一致性。
    """
    diag = [0.0] * len(points)
    rows = [{} for _ in points]
    mass = [0.0] * len(points)
    weakened = set()
    for face in triangles:
        a, b, c = [points[k] for k in face]
        twice = (b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])
        if abs(twice) < 1e-8:
            raise ValueError("残差积分遇到退化三角形")
        area = abs(twice) / 2
        gradients = [
            ((b[1] - c[1]) / twice, (c[0] - b[0]) / twice),
            ((c[1] - a[1]) / twice, (a[0] - c[0]) / twice),
            ((a[1] - b[1]) / twice, (b[0] - a[0]) / twice),
        ]
        factors = []
        for i, j in zip(face, (*face[1:], face[0]), strict=True):
            factor = (0.25 if jump(anchors[i], anchors[j]) else 1.0) * smoothing_factor(
                points[i], points[j], clues
            )
            factors.append(factor)
            if factor < 1:
                weakened.add(tuple(sorted((i, j))))
        # 整个局部单元采用同一个正系数，保持梯度能量非负，不将线索当墙切开。
        factor = min(factors)
        for i, k in enumerate(face):
            mass[k] += 0.35 * area / (3 * 2500)
            for j, other in enumerate(face):
                term = (
                    2
                    * factor
                    * area
                    * sum(x * y for x, y in zip(gradients[i], gradients[j], strict=True))
                )
                if i == j:
                    diag[k] += term
                else:
                    rows[k][other] = rows[k].get(other, 0.0) + term
    return [a + b for a, b in zip(diag, mass, strict=True)], rows, mass, len(weakened)
