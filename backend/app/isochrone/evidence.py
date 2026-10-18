"""矩阵测时只有在同路段、近端点、等待口径得到双重支持时才作弱观测。"""

import math
from dataclasses import replace

from .geometry import haversine_m
from .routequality import usable_for_fusion


def calibrate_matrix_observations(chk, observations):
    from .validation import distance_to_paths

    anchors = []
    for p in chk.probes.values():
        if (
            p.kind.startswith("validation")
            or not usable_for_fusion(p, chk.center)
            or p.observed_location is None
            or p.raw_s is None
            or p.dist_m is None
            or p.route_m is None
            or p.route_s is None
            or not p.route_path
            or haversine_m(p.lat, p.lng, *p.observed_location) > 10
            or abs(p.dist_m - p.route_m) > 30
            or not -30 <= p.route_s - p.raw_s <= 600
        ):
            continue
        path = [chk.xy(*pos) for pos in p.route_path]
        anchors.append((p, chk.xy(*p.observed_location), list(zip(path, path[1:], strict=False))))
    matrix = {
        (round(chk.xy(p.lat, p.lng)[0], 6), round(chk.xy(p.lat, p.lng)[1], 6)): p
        for p in chk.probes.values()
        if p.raw_s is not None and p.dist_m is not None and not p.kind.startswith("validation")
    }
    calibrated = []
    for o in observations:
        p = matrix.get((round(o.x, 6), round(o.y, 6)))
        if o.protected or p is None or p.route_checked:
            calibrated.append(o)
            continue
        nearby = [
            (a, xy)
            for a, xy, segments in anchors
            if math.dist((o.x, o.y), xy) <= 100
            and distance_to_paths(o.x, o.y, segments) <= 2
            and abs(p.dist_m - a.route_m) <= 2 * math.dist((o.x, o.y), xy) + 30
        ]
        pair = next(
            (
                (a, b)
                for i, (a, xy) in enumerate(nearby)
                for b, other in nearby[i + 1 :]
                if math.dist(xy, other) >= 25
                and abs((a.route_s - a.raw_s) - (b.route_s - b.raw_s)) <= 30
                and abs(a.route_s - b.route_s) <= 120
                and abs(a.route_m - b.route_m) <= 2 * math.dist(xy, other) + 60
            ),
            None,
        )
        if pair:
            delay = sum(a.route_s - a.raw_s for a in pair) / 2
            calibrated.append(
                replace(o, seconds=max(0, p.raw_s + delay), weight=2.0, soft_eligible=True)
            )
        else:
            calibrated.append(o)
    return calibrated
