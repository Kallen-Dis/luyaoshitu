"""时限与同次求解复用；证据变化、状态隔离及阶段耗时的零配额回归。"""

from copy import deepcopy

import pytest
from test_gridcheck import POLY, C, FakeNet, _rays

from app.isochrone import fusion
from app.isochrone.gridcheck import GridcheckConfig, _Checker


def test_default_timeout_and_explicit_override(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "ENV_PATH", tmp_path / "missing.env")
    monkeypatch.delenv("BAIDU_ANALYSIS_TIMEOUT", raising=False)
    assert config.get_settings.__wrapped__().analysis_timeout_s == 600
    monkeypatch.setenv("BAIDU_ANALYSIS_TIMEOUT", "900")
    assert config.get_settings.__wrapped__().analysis_timeout_s == 900


def test_timeout_reports_stage_duration_and_honest_cache_semantics(monkeypatch):
    from app import main

    now = [10.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: now[0])
    state = main.AnalysisState()
    now[0] = 20.0
    state.enter("gridcheck")
    state.finish("等时圈")
    now[0] = 35.0
    detail = main._timeout_detail(600, state)
    assert detail["timings"] == {"elapsed_s": 25, "stages_s": {"center": 10, "gridcheck": 15}}
    assert "600 秒" in detail["message"] and "重新执行建模" in detail["message"]
    assert "断点附近" not in detail["message"]


def test_identical_evidence_reuses_actual_field_without_adoption_state_leaking(monkeypatch):
    chk = _Checker(FakeNet(), C, POLY, None, GridcheckConfig(fusion_mode=True))
    samples = _rays(chk.client)
    original = fusion._estimate_fusion
    calls = []

    def calculate(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(fusion, "_estimate_fusion", calculate)
    first = fusion.estimate_fusion(chk, samples)
    saved = deepcopy(first)
    first["quality"]["ready"] = "changed_by_adoption"
    first["time_field"]["vertices"].clear()
    assert fusion.estimate_fusion(chk, samples) == saved
    assert len(calls) == 1


@pytest.mark.parametrize(
    "change", ["time", "path", "kind", "rays", "rollback", "caps", "origin", "clue"]
)
def test_relevant_changes_always_recompute(monkeypatch, change):
    chk = _Checker(FakeNet(), C, POLY, None, GridcheckConfig(fusion_mode=True))
    p = chk.add((0, 0), 0, "cell", *C)
    p.route_path = [C, (C[0] + 0.001, C[1])]
    samples = _rays(chk.client)
    calls = []

    def calculate(*args):
        calls.append(1)
        return {"quality": {"ready": True}}

    monkeypatch.setattr(fusion, "_estimate_fusion", calculate)
    fusion.estimate_fusion(chk, samples)
    if change == "time":
        p.route_s = 850
    elif change == "path":
        p.route_path[1] = (C[0] + 0.002, C[1])
    elif change == "kind":
        p.kind = "validation"
    elif change == "rays":
        a, b, t, d = samples[0]
        samples[0] = (a, b, t + 20, d)
    elif change == "rollback":
        chk.rollback_xy = [(100, 50)]
    elif change == "caps":
        chk.correction_caps = [(100, 50, 25)]
    elif change == "origin":
        chk.origin_preflight = {"passed": False}
    else:
        chk.fusion_clues = [{"status": "confirmed"}]
    fusion.estimate_fusion(chk, samples)
    assert len(calls) == 2
