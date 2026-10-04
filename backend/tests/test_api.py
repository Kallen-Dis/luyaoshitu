"""FastAPI 路由的零配额冒烟测试：不发任何百度请求。"""

import json

import pytest
from fastapi.testclient import TestClient

from app import storage
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "analyses.db")
    with TestClient(app) as c:
        yield c


def test_health_and_config(client):
    health = client.get("/api/health").json()
    assert health["status"] in {"ok", "degraded"}
    assert "server_ak_configured" in health and "warnings" in health
    cfg = client.get("/api/config").json()
    assert cfg["grid"] == {"spacing_m": 100.0, "layout": "polygon", "extent_m": 1500.0}
    assert cfg["closure_limits"]["max_count"] == 20
    assert {m["id"] for m in cfg["modes"]} >= {"walk", "ride"}


def test_samples_open_with_report_and_quality(client):
    items = client.get("/api/samples").json()["samples"]
    assert {s["id"] for s in items} >= {"isochrone-caoyang-15min", "isochrone-taopu-15min"}
    feature = client.get("/api/samples/isochrone-taopu-15min").json()
    props = feature["properties"]
    assert props["report"]["grid_basis"].startswith("真实路网")
    assert props["quality"]["directions"] == len(props["rays"])
    assert client.get("/api/samples/..%2F..%2Fsecret").status_code == 404


def test_demo_uses_the_same_grid_and_simulate_estimates(client):
    feature = client.get("/api/demo", params={"lat": 31.25, "lng": 121.42}).json()
    blind = feature["properties"]["blindspots"]
    # 与实时分析同一口径：只在 15 分钟圈内按 100 米布点
    assert blind["layout"] == "polygon" and blind["grid_spacing_m"] == 100.0
    assert blind["cell_count"] == len(blind["cells"]) >= 30
    assert all(c["in_circle"] for c in blind["cells"])
    missing = next(c for c in blind["cells"] if c["missing"])
    resp = client.post(
        "/api/simulate",
        json={
            "category": missing["missing"][0],
            "lat": missing["lat"],
            "lng": missing["lng"],
            "feature": feature,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["basis"] == "estimate"  # 离线模拟数据不调接口
    assert body["covered_count"] >= 1


def test_simulate_without_grid_is_400(client):
    feature = {"type": "Feature", "geometry": {}, "properties": {"minutes": 15}}
    resp = client.post(
        "/api/simulate", json={"category": "医药", "lat": 31.25, "lng": 121.42, "feature": feature}
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "no_grid"


def test_closure_radius_is_validated(client):
    resp = client.post(
        "/api/isochrone",
        json={
            "lat": 31.25,
            "lng": 121.42,
            "closures": [{"lat": 31.25, "lng": 121.42, "radius_m": 5000}],
        },
    )
    assert resp.status_code == 422


def test_missing_ak_still_serves_samples_but_reports_it(client, monkeypatch):
    """没配 AK：样例照常打开；实时计算返回具体的「未配置」说明，而不是笼统的 500。"""
    monkeypatch.setattr(app.state.baidu, "_ak", "")
    assert client.get("/api/samples/isochrone-taopu-15min").status_code == 200

    resp = client.post("/api/isochrone", json={"lat": 30.0, "lng": 120.0})
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["code"] == "missing_server_ak"
    assert detail["stage_label"] == "等时圈采样"
    assert "BAIDU_SERVER_AK" in detail["message"]

    stream = client.post("/api/isochrone/stream", json={"lat": 30.0, "lng": 120.0})
    assert "event: error" in stream.text
    assert "missing_server_ak" in stream.text


def test_quota_exhausted_during_isochrone_is_specific(client, monkeypatch):
    from app.baidu.errors import QuotaExhaustedError

    async def quota_request(endpoint, params, cost=1.0):
        raise QuotaExhaustedError(302, "天配额超限", endpoint)

    monkeypatch.setattr(app.state.baidu, "_request", quota_request)
    # 远离样例的坐标，保证缓存里没有、一定会发请求
    resp = client.post("/api/isochrone", json={"lat": 30.0, "lng": 120.0})
    assert resp.status_code == 429
    detail = resp.json()["detail"]
    assert detail["service"] == "批量算路（步行）"
    assert detail["stage"] == "isochrone"
    assert detail["reset"] == "北京时间次日 0 点"
    assert "等时圈采样" in detail["message"] and "302" in detail["message"]


def test_geocode_quota_message_offers_alternatives(client, monkeypatch):
    from app.baidu.errors import QuotaExhaustedError

    async def quota_request(endpoint, params, cost=1.0):
        raise QuotaExhaustedError(302, "天配额超限", endpoint)

    monkeypatch.setattr(app.state.baidu, "_request", quota_request)
    resp = client.get("/api/geocode", params={"address": "上海市普陀区桃浦镇"})
    assert resp.status_code == 429
    assert "直接输入坐标" in resp.json()["detail"]["message"]


def test_any_result_can_be_exported(client):
    """实时与历史结果也要能交付，不只是样例。"""
    feature = client.get("/api/demo", params={"lat": 31.25, "lng": 121.42}).json()
    md = client.post("/api/export", json={"feature": feature, "format": "md"})
    assert md.status_code == 200
    assert "圈内各类民生设施覆盖" in md.text
    assert "灰色区域" in md.text
    # 离线模拟结果导出后脱离界面，报告本身也得写明不是真实路网
    assert "离线模拟结果" in md.text
    zipped = client.post("/api/export", json={"feature": feature, "format": "zip"})
    assert zipped.status_code == 200
    assert zipped.headers["content-type"] == "application/zip"
    bad = client.post("/api/export", json={"feature": {}, "format": "md"})
    assert bad.status_code == 400


def test_sample_report_marks_gray_regions(client):
    feature = client.get("/api/samples/isochrone-taopu-15min").json()
    gray = feature["properties"]["report"]["gray_regions"]
    assert gray["region_count"] >= 1
    first = gray["regions"][0]
    assert first["id"] == "A" and first["rings"]
    assert {d["cause"] for d in first["diagnosis"]} <= {"barrier", "supply", "unknown"}


def test_config_exposes_agent_plan_state_but_never_the_token(client):
    cfg = client.get("/api/config").json()
    assert set(cfg["agent_plan"]) == {"configured"}
    assert "llm" not in cfg
    assert "sk-ap" not in json.dumps(cfg)


def test_narrate_and_export_carry_the_same_template_conclusion(client):
    feature = client.get("/api/demo", params={"lat": 31.25, "lng": 121.42}).json()
    body = client.post("/api/narrate", json={"feature": feature}).json()
    assert "【总体结论】" in body["text"] and "模拟数据" in body["text"]
    md = client.post("/api/export", json={"feature": feature, "format": "md"}).text
    assert "## 综合结论" in md and body["text"] in md


def test_recheck_without_baseline_is_400_and_explains_why(client):
    feature = client.get("/api/demo", params={"lat": 31.25, "lng": 121.42}).json()
    resp = client.post("/api/recheck", json={"feature": feature})
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "no_route_baseline"


def test_site_plan_on_simulated_data_only_estimates(client):
    feature = client.get("/api/demo", params={"lat": 31.25, "lng": 121.42}).json()
    # 模拟数据没有设施坐标；附近数量也清零，才能把缺口判成「供给缺口」
    feature["properties"]["coverage"]["nearby_categories"] = {}
    resp = client.post("/api/site-plan", json={"feature": feature, "category": "医药"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["verified"] is False and body["quota"]["matrix_pairs"] == 0
    assert body["best"]["simulation"]["basis"] == "estimate"


def test_site_plan_refuses_when_cause_is_unknown(client):
    """附近有药店但没有坐标：成因判不了，不能开「补设」。"""
    feature = client.get("/api/demo", params={"lat": 31.25, "lng": 121.42}).json()
    resp = client.post("/api/site-plan", json={"feature": feature, "category": "医药"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "no_site"
