"""v3 的行为约束：局部性、留出可复现性、可靠证据与嵌套等时圈。"""

import math

from app.isochrone.localfield import Observation, contour_field, fit_field, hole_evidence
from app.isochrone.tin import inside_ring

RING = [(600 * math.cos(k * math.tau / 36), 600 * math.sin(k * math.tau / 36)) for k in range(36)]
SAMPLES = [
    (r * math.cos(k * math.tau / 36), r * math.sin(k * math.tau / 36), 1.5 * r, r)
    for k in range(36)
    for r in (200, 400, 600, 800)
]


def inside(mesh, x, y):
    return sum(inside_ring(x, y, ring) for ring in mesh.rings) % 2 == 1


def test_prior_recovers_circle_without_invented_holes():
    vertices, triangles, quality = fit_field(RING, SAMPLES, [])
    surface = contour_field(vertices, triangles, 900)
    assert quality["ready"]
    assert len(surface.rings) == 1
    assert abs(surface.area_m2 / (math.pi * 600**2) - 1) < 0.015
    assert inside(surface, 0, 0)


def test_local_slow_observation_does_not_move_the_opposite_boundary():
    vertices, triangles, quality = fit_field(RING, SAMPLES, [Observation(450, 0, 1200, 80, True)])
    surface = contour_field(vertices, triangles, 900)
    assert quality["ready"]
    assert not inside(surface, 450, 0)
    assert inside(surface, -580, 0)
    assert not inside(surface, -620, 0)


def test_reachable_route_can_restore_an_underestimated_boundary():
    vertices, triangles, quality = fit_field(RING, SAMPLES, [Observation(650, 0, 750, 80, True)])
    assert quality["ready"]
    assert inside(contour_field(vertices, triangles, 900), 650, 0)


def test_contradictory_protected_observations_fail_quality_gate():
    *_, quality = fit_field(
        RING, SAMPLES, [Observation(450, 0, 600, 80, True), Observation(450, 0, 1200, 80, True)]
    )
    assert quality["constraint_conflicts"] > 0
    assert not quality["ready"]


def test_all_thresholds_share_one_field_and_are_nested():
    observations = [Observation(450, 0, 1200, 80, True), Observation(-450, 0, 490, 80, True)]
    vertices, triangles, _ = fit_field(RING, SAMPLES, observations)
    meshes = [contour_field(vertices, triangles, t) for t in (300, 600, 900)]
    for x in range(-700, 701, 35):
        for y in range(-700, 701, 35):
            five, ten, fifteen = [inside(m, x, y) for m in meshes]
            assert not five or ten
            assert not ten or fifteen


def test_observation_order_does_not_change_the_solution():
    observations = [Observation(450, 0, 1200, 80, True), Observation(-450, 0, 490, 80, True)]
    assert fit_field(RING, SAMPLES, observations) == fit_field(RING, SAMPLES, observations[::-1])


def test_two_second_overrun_is_fitted_without_forcing_an_interior_hole():
    observation = Observation(300, 0, 902, 80, True)
    vertices, triangles, quality = fit_field(RING, SAMPLES, [observation])
    surface = contour_field(vertices, triangles, 900)
    assert quality["protected_routes"] == quality["borderline_routes"] == 1
    assert quality["threshold_constraint_routes"]["900"] == 0
    assert quality["hard_constraint_routes"] == 1  # 对五/十分钟仍是明确的超时证据。
    assert len(surface.rings) == 1
    assert inside(surface, observation.x, observation.y)
    assert observation.seconds == 902  # 原测时不能改写成 900 秒以内。


def test_single_clear_overrun_is_preserved_but_cannot_certify_an_entire_hole():
    observations = [Observation(300, 0, 1200, 80, True)]
    vertices, triangles, quality = fit_field(RING, SAMPLES, observations)
    surface = contour_field(vertices, triangles, 900)
    evidence = hole_evidence(surface.rings, observations, 900)
    assert quality["hard_constraint_routes"] == 1
    assert not inside(surface, 300, 0)
    assert evidence["holes"] == evidence["unsupported_holes"] == 1
    assert evidence["supported_holes"] == 0


def test_hole_requires_separate_overrun_points_and_surrounding_reachable_evidence():
    hole = [(250, -50), (250, 50), (350, 50), (350, -50)]
    over = [Observation(275, 0, 1200, 80, True), Observation(325, 0, 1250, 80, True)]
    nearby = [Observation(x, y, 650, 80, True) for x, y in ((220, -80), (220, 80), (380, 80))]
    assert hole_evidence([hole], over, 900)["unsupported_holes"] == 1
    assert hole_evidence([hole], [over[0], over[0], *nearby], 900)["unsupported_holes"] == 1
    assert hole_evidence([hole], [*over, *nearby], 900)["supported_holes"] == 0
    assert hole_evidence([hole], [*over, *nearby], 900, stable_rings=[hole])["supported_holes"] == 1


def test_matrix_only_outlier_cannot_move_formal_boundary_without_route_support():
    original, faces, _ = fit_field(RING, SAMPLES, [])
    candidate, _, _ = fit_field(RING, SAMPLES, [Observation(300, 0, 2000)])
    assert candidate == original
    assert len(contour_field(candidate, faces, 900).rings) == 1


def test_local_correction_is_exactly_zero_beyond_evidence_radius():
    original, _, _ = fit_field(RING, SAMPLES, [])
    candidate, _, _ = fit_field(RING, SAMPLES, [Observation(450, 0, 1200, 80, True)])
    original_values = {(v.x, v.y): v.t for v in original}
    for b in candidate:
        if math.hypot(b.x - 450, b.y) >= 125 and (b.x, b.y) in original_values:
            assert original_values[b.x, b.y] == b.t


def test_unverified_or_borderline_observations_cannot_certify_a_hole():
    ring = [(250, -50), (250, 50), (350, 50), (350, -50)]  # clockwise hole
    for observation in (Observation(300, 0, 1200), Observation(300, 0, 902, 80, True)):
        evidence = hole_evidence([ring], [observation], 900)
        assert evidence["unsupported_holes"] == 1
        assert evidence["hole_evidence_points"] == [0]


def test_positive_weights_remove_old_overshoot_around_an_off_vertex_borderline_point():
    # 旧的交叉观测方程在此产生无证据孔洞；正权重拟合不再制造这个峰值。
    observations = [Observation(325, 25, 902, 80, True)]
    vertices, triangles, _ = fit_field(RING, SAMPLES, observations)
    surface = contour_field(vertices, triangles, 900)
    evidence = hole_evidence(surface.rings, observations, 900)
    assert evidence["holes"] == 0
    assert observations[0].seconds == 902


def test_route_envelope_limits_overshoot_without_clamping_overruns_to_900():
    observations = [Observation(325, 25, 970, 80, True)]
    vertices, triangles, quality = fit_field(RING, SAMPLES, observations)
    close = [v for v in vertices if math.hypot(v.x - 325, v.y - 25) <= 50]
    assert quality["bounded_vertices"] > 0
    assert max(v.t for v in close) <= 970
    assert not inside(contour_field(vertices, triangles, 900), 325, 25)


def test_nearby_reachable_routes_prevent_matrix_outlier_from_excavating_a_hole():
    observations = [
        Observation(320, 20, 866, 80, True),
        Observation(355, 35, 868, 80, True),
        Observation(340, 25, 1400, 6),
    ]
    vertices, triangles, _ = fit_field(RING, SAMPLES, observations)
    mesh = contour_field(vertices, triangles, 900)
    assert inside(mesh, 340, 25)
    assert all(v.t <= 868 for v in vertices if 300 <= v.x <= 350 and 0 <= v.y <= 50)


def test_smoothing_disconnect_requires_both_route_time_and_distance_evidence():
    from app.isochrone.localfield import route_jump

    a = Observation(400, 0, 700, 80, True, 734)
    b = Observation(450, 0, 1200, 80, True, 1339)
    assert route_jump(a, b)
    assert not route_jump(a, Observation(450, 0, 1200, 80, True, 760))
    assert not route_jump(a, Observation(450, 0, 720, 80, True, 1339))
    _, _, quality = fit_field(RING, SAMPLES, [a, b])
    assert quality["weakened_smoothing_edges"] > 0
    assert quality["disconnected_smoothing_edges"] == 0  # 跳变不再当作硬墙。


def test_slow_support_requires_clear_same_side_neighbors():
    from app.isochrone.localfield import correction_support
    from app.isochrone.tin import Vertex

    vertex = Vertex(240, 0, 0, None)  # 在单个慢点的 50 米影响范围之外。
    slow = Observation(300, 0, 940, 80, True)
    for seconds, protected in ((850, True), (920, True), (1000, False)):
        neighbor = Observation(450, 0, seconds, 80, protected)
        assert correction_support([vertex], [slow, neighbor]) == [0]
    assert correction_support([vertex], [slow, Observation(450, 0, 1000, 80, True)])[0] > 0


def test_no_evidence_recovers_exact_ray_boundary_without_mesh_only_difference_patches():
    from app.isochrone.tin import ring_area

    vertices, triangles, quality = fit_field(RING, SAMPLES, [])
    result = contour_field(vertices, triangles, 900)
    assert quality["prior_boundary_vertices"] > 0
    assert len(result.rings) == 1
    assert abs(result.area_m2 - abs(ring_area(RING))) < 1e-5


def test_duplicate_endpoint_records_cannot_strengthen_a_slow_peak():
    observation = Observation(325, 25, 970, 80, True, 1000)
    first = fit_field(RING, SAMPLES, [observation])
    duplicate = fit_field(RING, SAMPLES, [observation] * 4)
    assert first[:2] == duplicate[:2]
    assert duplicate[2]["duplicate_or_outside_observations"] == 3


def test_clear_slow_response_preserves_only_its_actual_node_classification():
    for seconds in (921, 970):
        observation = Observation(300, 0, seconds, 80, True)
        vertices, triangles, quality = fit_field(RING, SAMPLES, [observation])
        result = contour_field(vertices, triangles, 900)
        assert quality["single_slow_hard_floors"] == int(seconds > 930)
        assert all(
            v.t <= seconds + 1e-6 for v in vertices if abs(v.x - 300) < 1e-7 and abs(v.y) < 1e-7
        )
        if seconds == 921:
            assert inside(result, 300, 0)
        assert observation.seconds == seconds and quality["ready"]
