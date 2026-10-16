"""OSM WGS84 与 BD09 的本地近似换算；不能当作测绘级坐标校准。"""

import math

from ..poi.crosscheck import bd09_to_gcj02, gcj02_to_bd09


def shift(lat, lng):
    if not (72.004 <= lng <= 137.8347 and 0.8293 <= lat <= 55.8271):
        return 0.0, 0.0
    x, y = lng - 105, lat - 35
    a = -100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    b = 300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    wave = (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
    a += wave + (20 * math.sin(y * math.pi) + 40 * math.sin(y / 3 * math.pi)) * 2 / 3
    a += (160 * math.sin(y / 12 * math.pi) + 320 * math.sin(y * math.pi / 30)) * 2 / 3
    b += wave + (20 * math.sin(x * math.pi) + 40 * math.sin(x / 3 * math.pi)) * 2 / 3
    b += (150 * math.sin(x / 12 * math.pi) + 300 * math.sin(x / 30 * math.pi)) * 2 / 3
    rad = math.radians(lat)
    magic = 1 - 0.006693421622965943 * math.sin(rad) ** 2
    root = math.sqrt(magic)
    return (
        a * 180 / ((6378245 * (1 - 0.006693421622965943)) / (magic * root) * math.pi),
        b * 180 / (6378245 / root * math.cos(rad) * math.pi),
    )


def to_bd(lng, lat):
    dy, dx = shift(lat, lng)
    a, b = gcj02_to_bd09(lat + dy, lng + dx)
    return b, a


def to_wgs(lat, lng):
    glat, glng = bd09_to_gcj02(lat, lng)
    a, b = glat, glng
    for _ in range(8):
        dy, dx = shift(a, b)
        a, b = glat - dy, glng - dx
    return a, b
