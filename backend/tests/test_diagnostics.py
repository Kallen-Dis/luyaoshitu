"""报错要具体：哪个服务、停在哪一步、什么时候恢复、现在能做什么。"""

import asyncio

from app.baidu.errors import (
    ConfigurationError,
    IncompleteSamplingError,
    QuotaExhaustedError,
    config_hint,
    quota_hint,
    service_label,
)
from app.diagnostics import (
    DegradationLog,
    blindspot_warnings,
    coverage_warnings,
    error_detail,
    isochrone_warnings,
    traffic_summary,
)
from app.poi.catalog import by_name
from app.poi.collect import CategoryResult, CleanStats, CoverageResult, Poi
from app.report.blindspot import BlindspotConfig, identify_blindspots


def test_service_labels_name_the_actual_api():
    assert service_label("/routematrix/v2/walking") == "批量算路（步行）"
    assert service_label("/place/v2/search") == "地点检索"
    assert service_label("/directionlite/v1/walking") == "步行路线规划"


def test_quota_error_says_service_stage_reset_and_what_to_do():
    exc = QuotaExhaustedError(302, "天配额超限", "/routematrix/v2/walking")
    status, detail = error_detail(exc, "isochrone", [])
    assert status == 429
    assert detail["code"] == "quota_exhausted"
    assert detail["service"] == "批量算路（步行）"
    assert detail["stage_label"] == "等时圈采样"
    assert detail["reset"] == "北京时间次日 0 点"
    msg = detail["message"]
    assert "批量算路（步行）" in msg and "302" in msg and "次日 0 点" in msg
    assert "等时圈采样" in msg and "预生成样例" in msg


def test_quota_error_mentions_what_is_kept():
    exc = QuotaExhaustedError(302, "", "/place/v2/search")
    _, detail = error_detail(exc, "coverage", ["等时圈"])
    assert "已完成的等时圈保留在地图上" in detail["message"]


def test_permanent_quota_is_not_promised_to_reset():
    exc = QuotaExhaustedError(301, "", "/routematrix/v2/walking")
    _, detail = error_detail(exc, "isochrone")
    assert detail["reset"] is None
    assert "不会自动恢复" in quota_hint(exc)


def test_geocode_quota_suggests_coordinates_instead():
    exc = QuotaExhaustedError(302, "", "/geocoding/v3/")
    _, detail = error_detail(exc, "geocode")
    assert "直接输入坐标" in detail["message"]


def test_missing_ak_is_its_own_code():
    exc = ConfigurationError(5, "未配置 BAIDU_SERVER_AK", "/routematrix/v2/walking")
    status, detail = error_detail(exc, "isochrone")
    assert status == 503
    assert detail["code"] == "missing_server_ak"
    assert "BAIDU_SERVER_AK" in detail["message"] and ".env" in detail["message"]


def test_whitelist_error_explains_the_fix():
    exc = ConfigurationError(210, "APP IP校验失败", "/place/v2/search")
    assert "白名单" in config_hint(exc) and "地点检索" in config_hint(exc)
    _, detail = error_detail(exc, "coverage")
    assert detail["code"] == "configuration_error"


def test_incomplete_sampling_is_not_a_quota_error():
    exc = IncompleteSamplingError(
        ["walk 1x100 BaiduApiError: [x] status=401 瞬时并发超限"], "/routematrix/v2/walking"
    )
    status, detail = error_detail(exc, "isochrone", retries=3)
    assert status == 502
    assert detail["code"] == "sampling_incomplete"
    assert "重试 3 次" in detail["message"] and "不出圈" in detail["message"]


class _Client:
    def __init__(self):
        self.quota_events = []


def test_coverage_warning_names_quota_when_it_ran_out():
    client = _Client()
    log = DegradationLog(client)
    client.quota_events.append({"endpoint": "/place/v2/search", "status": 302})
    coverage_warnings(log, ["医药", "基础教育"])
    (item,) = log.as_list()
    assert item["stage"] == "coverage"
    assert "配额" in item["message"] and "医药、基础教育" in item["message"]


def test_route_warning_counts_missing_directions():
    log = DegradationLog(_Client())
    isochrone_warnings(log, {"delay": {"routes_requested": 36, "routes_ok": 30}})
    assert "6 个方向" in log.as_list()[0]["message"]


def test_traffic_summary_reports_age_and_stale_fallback():
    log = DegradationLog(_Client())
    info = traffic_summary(log, [0.0, 120.0, 1800.0], [1800.0], "QuotaExhaustedError", 600)
    assert info == {
        "fresh_ttl_min": 10.0,
        "max_age_min": 30.0,
        "stale_points": 1,
        "stale_max_age_min": 30.0,
    }
    assert "30.0 分钟前" in log.as_list()[0]["message"]
    assert traffic_summary(DegradationLog(_Client()), [], [], None, 600) is None


def test_blindspot_warning_and_incomplete_category():
    client = _Client()
    log = DegradationLog(client)
    client.quota_events.append({"endpoint": "/routematrix/v2/walking", "status": 302})
    blindspot_warnings(
        log,
        {
            "cells": [{"unknown": ["医药"]}, {"unknown": []}],
            "incomplete_categories": ["医药"],
        },
    )
    messages = [w["message"] for w in log.as_list()]
    assert any("配额" in m and "1 个方格" in m for m in messages)
    assert any("「医药」" in m for m in messages)


class _FailingMatrix:
    """中心到网格正常，逐格判定全部测不出——模拟配额在盲区判定途中用完。"""

    async def walking_matrix(self, origin, destinations):
        return [{"distance_m": 400.0, "duration_s": 300.0} for _ in destinations]

    async def walking_matrix_grid(self, origins, destinations):
        return [[None] * len(destinations) for _ in origins]


def test_category_with_too_few_judged_cells_gets_no_ratio():
    from app.isochrone.geometry import offset_point

    center = (31.248, 121.417)
    # 药店铺满整片网格，每一格直线 1 公里内都有候选，不会被直线下界直接判定
    pois = []
    for north in range(-1400, 1401, 700):
        for east in range(-1400, 1401, 700):
            lat, lng = offset_point(*offset_point(*center, 0, north), 90, east)
            pois.append(Poi(f"药店{north}_{east}", lat, lng, "医药"))
    coverage = CoverageResult(center=center, radius_m=2500)
    coverage.results.append(CategoryResult(category="医药", pois=pois, stats=CleanStats()))
    result = asyncio.run(
        identify_blindspots(
            _FailingMatrix(),
            center,
            [],
            coverage,
            # 不传等时圈，用圆形布点铺出一片网格：这里只关心「判定不足一半不给占比」
            BlindspotConfig(layout="disc", grid_spacing_m=200.0),
            categories=(by_name("医药"),),
        )
    )
    assert "医药" not in result.blind_ratio
    assert result.incomplete_categories == ["医药"]
