"""独立门店反馈：去重、时效、空间匹配、并发和接口隔离。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import main, storage
from app.markings import routes as marking_routes
from app.markings import store as marking_store
from app.markings import trust
from app.trip.feedback import FreshFeedbackStore

PLACE = {"name": "联华超市（曹杨店）", "category": "生鲜采买", "lat": 31.25, "lng": 121.42}
NOW = datetime(2026, 10, 8, tzinfo=timezone(timedelta(hours=8)))
AUTHOR = {"X-Device-Id": "feedback-author-00000001"}
OTHER = {"X-Device-Id": "feedback-other-000000002"}


def test_one_device_one_observation_change_and_retract(tmp_path):
    store = FreshFeedbackStore(tmp_path)
    assert store.summaries([PLACE], "a", NOW)[0]["confirms"] == 0
    store.vote(PLACE, "a", 1, NOW)
    assert store.vote(PLACE, "a", 1, NOW)["confirms"] == 1
    assert store.vote(PLACE, "b", 1, NOW)["confirms"] == 2
    changed = store.vote(PLACE, "a", -1, NOW)
    assert (changed["confirms"], changed["not_seen"], changed["my_feedback"]) == (1, 1, -1)
    assert store.summaries([PLACE], "b", NOW)[0]["my_feedback"] == 1
    withdrawn = store.vote(PLACE, "a", 0, NOW)
    assert (withdrawn["confirms"], withdrawn["not_seen"], withdrawn["my_feedback"]) == (1, 0, 0)
    assert store.vote({**PLACE, "name": "不存在的门店"}, "a", 0, NOW)["confirms"] == 0


def test_recent_window_does_not_count_old_observations(tmp_path):
    store = FreshFeedbackStore(tmp_path)
    store.vote(PLACE, "old", 1, NOW - timedelta(days=91))
    store.vote(PLACE, "edge", -1, NOW - timedelta(days=90))
    current = store.summaries([PLACE], "old", NOW)[0]
    assert (current["confirms"], current["not_seen"], current["my_feedback"]) == (0, 1, 0)
    assert store.vote(PLACE, "old", 1, NOW)["confirms"] == 1


def test_name_and_coordinate_matching_preserves_branches(tmp_path):
    store = FreshFeedbackStore(tmp_path)
    store.vote(PLACE, "a", 1, NOW)
    near = {**PLACE, "name": " 联华超市 (曹杨店) ", "lat": PLACE["lat"] + 0.0001}
    assert store.summaries([near], None, NOW)[0]["confirms"] == 1
    far = {**PLACE, "lat": PLACE["lat"] + 0.001}
    different = {**PLACE, "name": "联华超市（桃浦店）"}
    assert [s["confirms"] for s in store.summaries([far, different], None, NOW)] == [0, 0]


def test_parallel_new_store_votes_merge_atomically(tmp_path):
    store = FreshFeedbackStore(tmp_path)
    store.summaries([PLACE], None, NOW)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(lambda n: FreshFeedbackStore(tmp_path).vote(PLACE, str(n), 1, NOW), range(16))
        )
    assert store.summaries([PLACE], None, NOW)[0]["confirms"] == 16


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "analyses.db")
    monkeypatch.setattr(marking_store, "DATA_DIR", tmp_path / "markings")
    monkeypatch.setattr(marking_routes, "_service", None)
    with TestClient(main.app) as client:
        yield client


def test_api_feedback_is_not_a_facility_and_summary_is_read_only(client, monkeypatch):
    svc = marking_routes.get_service()
    # 即使算路客户端不可用，独立反馈接口也应正常工作。
    monkeypatch.setattr(main.app.state, "baidu", None)
    request = {"place": PLACE, "vote": 1}
    assert client.put("/api/trip/fresh-feedback", json=request).status_code == 400
    written = client.put("/api/trip/fresh-feedback", json=request, headers=AUTHOR)
    assert written.status_code == 200 and written.json()["my_feedback"] == 1
    summaries = client.post(
        "/api/trip/fresh-feedback/summary", json={"places": [PLACE]}, headers=OTHER
    )
    assert summaries.status_code == 200
    assert summaries.json()["items"][0]["confirms"] == 1
    assert summaries.json()["items"][0]["my_feedback"] == 0
    assert (
        client.get("/api/markings", params={"lat": 31.25, "lng": 121.42}, headers=AUTHOR).json()[
            "items"
        ]
        == []
    )
    assert not (svc.store.root / "trip-budget.sqlite3").exists()
    assert (
        client.put("/api/trip/fresh-feedback", json={**request, "vote": 0}, headers=AUTHOR).json()[
            "confirms"
        ]
        == 0
    )


@pytest.mark.parametrize(
    "change", [{"name": " \u200b "}, {"category": "医药"}, {"lat": 80}, {"lng": 0}]
)
def test_invalid_places_rejected(client, change):
    result = client.put(
        "/api/trip/fresh-feedback", json={"place": {**PLACE, **change}, "vote": 1}, headers=AUTHOR
    )
    assert result.status_code == 422


@pytest.mark.parametrize("vote", [True, "1", 2, -2])
def test_invalid_votes_rejected(client, vote):
    assert (
        client.put(
            "/api/trip/fresh-feedback", json={"place": PLACE, "vote": vote}, headers=AUTHOR
        ).status_code
        == 422
    )


def test_write_and_summary_limits(client, monkeypatch):
    monkeypatch.setitem(trust.LIMITS, "vote", (1, 200))
    request = {"place": PLACE, "vote": 1}
    assert client.put("/api/trip/fresh-feedback", json=request, headers=AUTHOR).status_code == 200
    limited = client.put("/api/trip/fresh-feedback", json=request, headers=AUTHOR)
    assert limited.status_code == 429 and "retry-after" in limited.headers
    svc = marking_routes.get_service()
    for _ in range(600):
        svc.limiter.hit("fresh-summary", "i:testclient", 600, 3600)
    limited = client.post("/api/trip/fresh-feedback/summary", json={"places": [PLACE]})
    assert limited.status_code == 429
