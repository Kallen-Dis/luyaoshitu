"""标注接口的端到端冒烟：设备头、编辑令牌、原始字节上传照片、照片响应头、审核口令。"""

import pytest
from fastapi.testclient import TestClient

from app import storage
from app.main import app
from app.markings import routes
from app.markings import service as service_mod
from app.markings import store as store_mod
from tests.test_markings_photos import jpeg, png

AUTHOR = {"X-Device-Id": "device-author-0000000001"}
OTHER = {"X-Device-Id": "device-other-00000000002"}
ADMIN = {"X-Device-Id": "device-admin-00000000009", "X-Admin-Token": "admin-token-for-tests"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "analyses.db")
    monkeypatch.setattr(store_mod, "DATA_DIR", tmp_path / "user")
    monkeypatch.setattr(routes, "_service", None)
    monkeypatch.setattr(service_mod, "REVIEW_MIN_SECONDS", 0.0)
    with TestClient(app) as c:
        routes.get_service().admin_token = ADMIN["X-Admin-Token"]
        yield c


CLOSURE = {"type": "closure", "lat": 31.25, "lng": 121.42, "radius_m": 60, "note": "路口围挡"}


def stage(client, headers, image=None) -> str:
    """新建前先传现场照片，拿到预传 ID。"""
    resp = client.post(
        "/api/markings/uploads",
        content=image or png(width=77, height=33),
        headers={**headers, "Content-Type": "image/png"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def create(client, headers, body=CLOSURE):
    payload = {**body, "photos": [stage(client, headers)]}
    return client.post("/api/markings", json=payload, headers=headers)


def test_full_lifecycle_through_http(client):
    cfg = client.get("/api/config").json()["markings"]
    assert cfg["admin_enabled"] is True and cfg["photo"]["max_per_marking"] == 6
    assert cfg["photo"]["required"] is True and cfg["photo"]["min_per_marking"] == 1
    assert "construction" in cfg["closure_kinds"]

    no_photo = client.post("/api/markings", json=CLOSURE, headers=AUTHOR)
    assert no_photo.status_code == 400 and no_photo.json()["detail"]["code"] == "photo_required"
    created = create(client, AUTHOR)
    assert created.status_code == 201 and created.json()["marking"]["photo_count"] == 1
    token = created.json()["edit_token"]
    mid = created.json()["marking"]["id"]
    edit = {**AUTHOR, "X-Edit-Token": token}

    near = client.get("/api/markings", params={"lat": 31.25, "lng": 121.42}, headers=OTHER)
    assert [m["id"] for m in near.json()["items"]] == [mid]
    assert near.json()["items"][0]["mine"] is False

    up = client.post(
        f"/api/markings/{mid}/photos",
        content=jpeg(),
        headers={**edit, "Content-Type": "image/jpeg"},
    )
    assert up.status_code == 201
    photo = up.json()
    got = client.get(photo["url"])
    assert got.status_code == 200 and got.headers["content-type"] == "image/jpeg"
    assert got.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in got.headers["content-security-policy"]
    assert b"GPSLatitude" not in got.content

    url = f"/api/markings/{mid}"
    patched = client.patch(url, json={"version": 1, "radius_m": 80}, headers=edit)
    assert patched.status_code == 200 and patched.json()["version"] == 2
    denied = client.patch(url, json={"version": 2, "radius_m": 90}, headers=OTHER)
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "forbidden"

    assert (
        client.put(f"/api/markings/{mid}/vote", json={"vote": 1}, headers=OTHER).json()["confirms"]
        == 1
    )

    assert client.get("/api/admin/markings", headers=OTHER).status_code == 401
    queue = client.get("/api/admin/markings", headers=ADMIN).json()
    assert [m["id"] for m in queue["items"]] == [mid]
    opened = client.get(f"/api/admin/markings/{mid}", headers=ADMIN).json()
    review = {
        "decision": "verify",
        "version": opened["version"],
        "note": "照片清楚，现场确有围挡",
        "basis": ["photo"],
        "checks": {"location": True, "type": True, "current": True},
        "photos_reviewed": [p["id"] for p in opened["photos"]],
    }
    done = client.post(f"/api/admin/markings/{mid}/review", json=review, headers=ADMIN)
    assert done.status_code == 200 and done.json()["status"] == "verified"

    gone = client.delete(f"/api/markings/{mid}", headers=edit)
    assert gone.json()["status"] == "retracted"
    back = client.post(f"/api/markings/{mid}/restore", headers=edit)
    assert back.json()["status"] == "verified"

    mine = client.get("/api/markings/mine", headers=AUTHOR).json()["items"]
    assert [m["id"] for m in mine] == [mid]
    assert client.delete(f"/api/markings/photos/{photo['id']}", headers=edit).status_code == 204
    assert client.get(photo["url"]).status_code == 404
    # 只剩新建时附的那一张了：作者不能把它也删掉
    last = client.get(f"/api/markings/{mid}", headers=AUTHOR).json()["photos"]
    assert len(last) == 1
    blocked = client.delete(f"/api/markings/photos/{last[0]['id']}", headers=edit)
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "photo_last"


def test_upload_limits_and_validation_errors_are_readable(client):
    mid = create(client, AUTHOR).json()["marking"]["id"]
    too_big = b"\xff\xd8\xff" + b"0" * (4 * 1024 * 1024)
    for url in (f"/api/markings/{mid}/photos", "/api/markings/uploads"):
        big = client.post(url, content=too_big, headers={**OTHER, "Content-Type": "image/jpeg"})
        assert big.status_code == 413 and "4 MB" in big.json()["detail"]["message"]
    no_device = client.post(
        "/api/markings/uploads", content=jpeg(), headers={"Content-Type": "image/jpeg"}
    )
    assert no_device.status_code == 400 and no_device.json()["detail"]["code"] == "missing_device"
    bad = client.post("/api/markings", json={**CLOSURE, "radius_m": 900}, headers=OTHER)
    assert bad.status_code == 400 and "半径" in bad.json()["detail"]["message"]
    nodevice = client.post("/api/markings", json=CLOSURE)
    assert nodevice.status_code == 400
    assert client.get("/api/markings/photos/../../etc").status_code == 404
    assert client.get("/api/markings/photos/" + "0" * 32).status_code == 404


def test_admin_endpoints_are_closed_without_token(client):
    routes.get_service().admin_token = ""
    resp = client.get("/api/admin/markings", headers=ADMIN)
    assert resp.status_code == 503 and resp.json()["detail"]["code"] == "admin_disabled"
