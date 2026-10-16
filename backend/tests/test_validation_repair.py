"""目标拥挤时改选点，不降低验收间距、不修改候选范围。"""

import math

from app.isochrone.validation import select_spaced_targets


def test_bounded_repair_covers_all_regions_without_relaxing_distance():
    pools = [
        [(11, 11), (9, 13), (47, 26)],
        [(10, 6), (46, 27), (20, 4)],
        [(40, 19), (15, 9), (15, 10)],
        [(20, 23), (44, 15), (36, 34)],
    ]
    candidates = [
        dict(key=(r, j), xy=p, region=r, sector=0, source="changed_patch", threshold_distance=0)
        for r, pool in enumerate(pools)
        for j, p in enumerate(pool)
    ]
    selected, regions, _ = select_spaced_targets(candidates, 4)
    assert regions == {0, 1, 2, 3} and len(selected) == 4
    assert all(
        math.dist(a["xy"], b["xy"]) >= 10 for i, a in enumerate(selected) for b in selected[i + 1 :]
    )


def test_physically_impossible_coverage_is_still_reported_missing():
    candidates = [
        dict(
            key=(r, 0), xy=(r, 0), region=r, sector=0, source="changed_patch", threshold_distance=0
        )
        for r in range(3)
    ]
    selected, regions, _ = select_spaced_targets(candidates, 3)
    assert len(selected) == len(regions) == 1
