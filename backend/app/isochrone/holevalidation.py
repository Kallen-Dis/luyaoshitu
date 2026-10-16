"""冻结前安排逐孔洞留出目标；覆盖按实际终点核对，不借附近道路冒充孔内。"""

import math

from .geometry import METERS_PER_DEG_LAT
from .holepolicy import distance_to_ring
from .tin import inside_ring


def task_contains(task, point):
    ring = task["ring_xy"]
    inside = inside_ring(*point, ring)
    if task["role"] == "inside":
        return inside
    center = task["center_xy"]
    sector = int(
        (math.atan2(point[1] - center[1], point[0] - center[0]) % math.tau) / (math.pi / 2)
    )
    return not inside and distance_to_ring(point, ring) <= 100 and sector == task["sector"]


def add_hole_targets(chk, fusion, selections):
    from .validation import distance_to_paths, known_reachable_segments

    paths = known_reachable_segments(chk, fusion)
    training = [
        chk.xy(p["lat"], p["lng"]) for p in fusion["measured_points"] if p["time_s"] is not None
    ]
    reserved = [q["xy"] for q in selections]
    reserved += [q["xy"] for s in selections for q in s.get("alternatives", [])]
    tasks, missing = [], []
    for record in fusion.get("quality", {}).get("hole_records", []):
        if record.get("status") == "geographic":
            continue
        ring = record["ring_xy"]
        center = tuple(sum(p[k] for p in ring) / len(ring) for k in (0, 1))
        x0, x1 = min(p[0] for p in ring) - 100, max(p[0] for p in ring) + 100
        y0, y1 = min(p[1] for p in ring) - 100, max(p[1] for p in ring) + 100
        # 有界计算搜索；没有合格位置必须报告未完成，不能降低独立性距离。
        step = max(10.0, math.sqrt((x1 - x0) * (y1 - y0) / 5000))
        pool = [
            (x0 + i * step, y0 + j * step)
            for i in range(int((x1 - x0) / step) + 1)
            for j in range(int((y1 - y0) / step) + 1)
        ]
        pool += [
            tuple(sum(fusion["time_field"]["vertices_xy"][i][k] for i in face) / 3 for k in (0, 1))
            for face in fusion["time_field"]["triangles"]
        ]
        pool = sorted(set(pool), key=lambda p: (math.dist(p, center), p))
        roles = [("inside", i) for i in range(max(2, math.ceil(record["area_m2"] / 10000)))]
        # 四方向均安排，未被线索包围的开口方向也要检验。
        roles += [("outside", i) for i in range(4)]
        for role, index in roles:
            task = {
                "id": f"hole-{record['id']}-{role}-{index}",
                "hole_id": record["id"],
                "role": role,
                "sector": index,
                "ring_xy": ring,
                "center_xy": center,
            }
            tasks.append(task)
            eligible = [
                p
                for p in pool
                if task_contains(task, p)
                and all(math.dist(p, t) >= 10 for t in training)
                and all(math.dist(p, r) >= (25 if role == "inside" else 10) for r in reserved)
                and distance_to_paths(*p, paths) > 5
            ]
            if not eligible:
                missing.append(task["id"])
                continue

            def target(p, serial, task=task):
                x, y = p
                return {
                    "key": (920000 + len(tasks), serial),
                    "xy": p,
                    "source": "hole_holdout",
                    "lat": chk.center[0] + y / METERS_PER_DEG_LAT,
                    "lng": chk.center[1] + x / chk.mpl,
                    "region": None,
                    "sector": int((math.atan2(y, x) % math.tau) / (math.pi / 2)),
                    "region_nodes": [],
                    "region_triangles": [],
                    "region_polygons": [],
                    "hole_task": task,
                }

            q = target(eligible[0], 0)
            backup = next((p for p in eligible[1:] if math.dist(p, eligible[0]) >= 10), None)
            q["alternatives"] = [target(backup, 1)] if backup else []
            selections.append(q)
            reserved.append(eligible[0])
            if backup:
                reserved.append(backup)
    return tasks, missing
