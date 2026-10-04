"""标注业务规则：新建与去重、修改与撤销、投票、照片（必填）、管理员审核（不能一键核实）。"""

import itertools
import math
from datetime import timedelta

import pytest

from app.config import Settings
from app.markings import service as service_mod
from app.markings.models import now_beijing
from app.markings.service import MarkingError, MarkingService
from app.markings.store import MarkingStore
from tests.test_markings_photos import jpeg, png

LAT0, LNG0 = 31.25, 121.42
ADMIN = "admin-token-for-tests"


def make_service(tmp_path, admin=ADMIN) -> MarkingService:
    settings = Settings(server_ak="", browser_ak="", admin_token=admin, marking_salt="salt")
    return MarkingService(MarkingStore(tmp_path), settings)


@pytest.fixture()
def svc(tmp_path):
    return make_service(tmp_path)


def v(svc, device="device-aaaaaaaaaaaaaaaa", ip="10.0.0.1"):
    return svc.viewer(device, ip)


AUTHOR = "device-author-0000000001"
OTHER = "device-other-00000000002"
THIRD = "device-third-00000000003"
ADMIN_DEVICE = "device-admin-00000000009"


def closure(**kw):
    return {"type": "closure", "lat": LAT0, "lng": LNG0, "radius_m": 60, **kw}


def north(m):
    return LAT0 + m / 111_320


_shots = itertools.count()


def shot() -> bytes:
    """每次一张内容不同的现场照片（同一条标注里重复的照片会被拒）。高度 31 与其他用例的图都不同。"""
    return png(width=40 + next(_shots), height=31)


def create(svc, payload, device=AUTHOR, photos=1):
    """按真实流程新建：先传照片拿预传 ID，再带着它新建。"""
    viewer = v(svc, device)
    ids = [svc.stage_photo(shot(), viewer)["id"] for _ in range(photos)]
    return svc.create({**payload, "photos": ids}, viewer)


def test_create_returns_token_and_appears_nearby_for_everyone(svc):
    out = create(svc, closure(note="景泰路西段"))
    token, m = out["edit_token"], out["marking"]
    assert len(token) >= 40 and m["status"] == "pending" and m["mine"]
    assert m["title"] == "施工围挡 · 半径 60 米"
    near = svc.nearby(LAT0, LNG0, 500, v(svc, OTHER))
    assert [x["id"] for x in near] == [m["id"]] and near[0]["mine"] is False
    assert near[0]["distance_m"] == 0
    assert svc.nearby(north(2000), LNG0, 500, v(svc, OTHER)) == []


def test_missing_device_is_rejected(svc):
    with pytest.raises(MarkingError) as info:
        svc.create(closure(), svc.viewer("bad id!", "1.1.1.1"))
    assert info.value.code == "missing_device"


def test_duplicate_is_flagged_but_can_be_forced(svc):
    create(svc, closure())
    other = v(svc, OTHER)
    ids = [svc.stage_photo(shot(), other)["id"]]
    with pytest.raises(MarkingError) as info:
        svc.create(closure(lat=north(10), photos=ids), other)
    assert info.value.status == 409 and info.value.extra["existing"]["id"]
    # 被提示重复后坚持提交：刚才传的照片还在预传区，直接复用，不必重传
    forced = svc.create(closure(lat=north(10), force=True, photos=ids), other)
    assert forced["marking"]["id"] != info.value.extra["existing"]["id"]
    assert forced["marking"]["photo_count"] == 1


def test_update_needs_token_and_version_and_unverifies(svc):
    out = create(svc, closure())
    mid, token = out["marking"]["id"], out["edit_token"]
    with pytest.raises(MarkingError) as info:
        svc.update(mid, {"radius_m": 80}, 1, "wrong", v(svc, AUTHOR))
    assert info.value.status == 403
    updated = svc.update(mid, {"radius_m": 80}, 1, token, v(svc, AUTHOR))
    assert updated["version"] == 2 and updated["spec"]["radius_m"] == 80
    with pytest.raises(MarkingError) as info:
        svc.update(mid, {"radius_m": 90}, 1, token, v(svc, AUTHOR))
    assert info.value.code == "version_conflict" and info.value.extra["current_version"] == 2
    with pytest.raises(MarkingError, match="没有需要保存"):
        svc.update(mid, {"radius_m": 80}, 2, token, v(svc, AUTHOR))

    # 已核实的标注内容一改就要重新核实；只续期不影响核实状态
    svc.store.transition(
        mid,
        from_statuses=("pending",),
        to_status="verified",
        now="2026-01-01T00:00:00+08:00",
        actor="admin",
        actor_hash=None,
        action="verify",
    )
    renewed = svc.update(mid, {"expires_in_days": 30}, 2, token, v(svc, AUTHOR))
    assert renewed["status"] == "verified" and renewed["version"] == 2
    edited = svc.update(mid, {"note": "改了说明"}, 2, token, v(svc, AUTHOR))
    assert edited["status"] == "pending" and edited["version"] == 3


def test_retract_restore_and_revert(svc):
    out = create(svc, closure())
    mid, token = out["marking"]["id"], out["edit_token"]
    svc.update(mid, {"radius_m": 120}, 1, token, v(svc, AUTHOR))
    retracted = svc.retract(mid, token, v(svc, AUTHOR))
    assert retracted["status"] == "retracted"
    assert svc.nearby(LAT0, LNG0, 500, v(svc, OTHER)) == []
    with pytest.raises(MarkingError) as info:
        svc.detail(mid, v(svc, OTHER))  # 撤回的不再对别人展示
    assert info.value.status == 404
    with pytest.raises(MarkingError, match="恢复"):
        svc.update(mid, {"radius_m": 90}, 2, token, v(svc, AUTHOR))
    restored = svc.restore(mid, token, v(svc, AUTHOR))
    assert restored["status"] == "pending"
    reverted = svc.revert(mid, 1, 2, token, v(svc, AUTHOR))
    assert reverted["version"] == 3 and reverted["spec"]["radius_m"] == 60
    detail = svc.detail(mid, v(svc, AUTHOR))
    assert [x["version"] for x in detail["versions"]] == [1, 2, 3]
    actions = [e["action"] for e in detail["events"]]
    assert actions == ["create", "edit", "retract", "restore", "revert"]


def test_restore_keeps_verification_only_if_content_unchanged(svc):
    out = create(svc, closure())
    mid, token = out["marking"]["id"], out["edit_token"]
    svc.store.transition(
        mid,
        from_statuses=("pending",),
        to_status="verified",
        now="2026-01-01T00:00:00+08:00",
        actor="admin",
        actor_hash=None,
        action="verify",
    )
    svc.retract(mid, token, v(svc, AUTHOR))
    assert svc.restore(mid, token, v(svc, AUTHOR))["status"] == "verified"


def test_votes_are_one_per_device_and_not_on_own_marking(svc):
    mid = create(svc, closure())["marking"]["id"]
    with pytest.raises(MarkingError) as info:
        svc.vote(mid, 1, v(svc, AUTHOR))
    assert info.value.code == "own_marking"
    svc.vote(mid, 1, v(svc, OTHER))
    again = svc.vote(mid, 1, v(svc, OTHER))
    assert again["confirms"] == 1 and again["my_vote"] == 1
    svc.vote(mid, -1, v(svc, OTHER))
    svc.vote(mid, -1, v(svc, THIRD))
    m = svc.public(mid, v(svc, THIRD))
    assert (m["confirms"], m["disputes"], m["disputed"]) == (0, 2, True)
    assert m["status"] == "pending"  # 票数永远不会让标注自动生效
    assert svc.vote(mid, 0, v(svc, THIRD))["disputes"] == 1


def test_expired_markings_drop_out(svc):
    mid = create(svc, closure())["marking"]["id"]
    past = (now_beijing() - timedelta(days=1)).isoformat(timespec="seconds")
    con = svc.store._connect()
    con.execute("UPDATE markings SET expires_at=? WHERE id=?", (past, mid))
    con.close()
    assert svc.public(mid, v(svc, AUTHOR))["status"] == "expired"
    assert svc.nearby(LAT0, LNG0, 500, v(svc, OTHER)) == []
    with pytest.raises(MarkingError, match="已过期"):
        svc.vote(mid, 1, v(svc, OTHER))


def test_photos_roles_limits_dedupe_and_delete(svc):
    out = create(svc, closure())
    mid, token = out["marking"]["id"], out["edit_token"]
    own = svc.add_photo(mid, jpeg(), token, v(svc, AUTHOR))
    assert own["role"] == "author" and own["url"].endswith(own["id"])
    with pytest.raises(MarkingError) as info:
        svc.add_photo(mid, jpeg(), None, v(svc, OTHER))
    assert info.value.code == "photo_duplicate"
    witness = svc.add_photo(mid, png(), None, v(svc, OTHER))
    assert witness["role"] == "witness"
    path, mime = svc.photo_path(witness["id"])
    assert path.exists() and mime == "image/png"
    for w in (100, 200):
        svc.add_photo(mid, png(width=w + 16), None, v(svc, OTHER))
    with pytest.raises(MarkingError) as info:
        svc.add_photo(mid, png(width=500), None, v(svc, OTHER))
    assert info.value.code == "photo_uploader"
    with pytest.raises(MarkingError):
        svc.delete_photo(own["id"], None, v(svc, THIRD))
    svc.delete_photo(witness["id"], None, v(svc, OTHER))
    assert not path.exists()
    with pytest.raises(MarkingError):
        svc.photo_path(witness["id"])
    # 新建时附的 1 张 + 作者补的 1 张 + 其他人剩下的 2 张
    assert svc.public(mid, v(svc, AUTHOR))["photo_count"] == 4


def test_site_photo_is_required_and_attached_in_the_same_transaction(svc):
    author = v(svc, AUTHOR)
    with pytest.raises(MarkingError) as info:
        svc.create(closure(), author)
    assert info.value.code == "photo_required" and info.value.extra["field"] == "photos"
    assert svc.nearby(LAT0, LNG0, 500, author) == []

    # 别人传的照片不能拿来用
    foreign = svc.stage_photo(shot(), v(svc, OTHER))["id"]
    with pytest.raises(MarkingError) as info:
        svc.create(closure(photos=[foreign]), author)
    assert info.value.code == "photo_expired" and info.value.extra["missing"] == [foreign]

    # 同一张图传两次：同一条标注里不收重复的
    same = shot()
    twins = [svc.stage_photo(same, author)["id"] for _ in range(2)]
    with pytest.raises(MarkingError) as info:
        svc.create(closure(photos=twins), author)
    assert info.value.code == "photo_duplicate"

    upload = svc.stage_photo(shot(), author)
    out = svc.create(closure(photos=[upload["id"]]), author)
    m = out["marking"]
    assert m["photo_count"] == 1
    photo = svc.store.photos(m["id"])[0]
    assert photo["id"] == upload["id"] and photo["role"] == "author"
    assert svc.store.uploads([upload["id"]]) == {}  # 已从预传区挪走
    assert svc.photo_path(upload["id"])[0].exists()
    # 已经挂上的照片不能再拿去建第二条
    with pytest.raises(MarkingError) as info:
        svc.create(closure(lat=north(800), photos=[upload["id"]]), author)
    assert info.value.code == "photo_expired"


def test_store_rolls_back_the_marking_when_an_upload_is_missing(svc):
    from app.markings import models
    from app.markings.store import UploadMissing

    spec, columns = models.normalize(closure())
    with pytest.raises(UploadMissing):
        svc.store.insert(
            spec,
            columns,
            source="user",
            author_hash="h",
            edit_hash="e",
            now="2026-10-01T10:00:00+08:00",
            expires_at="2026-12-01T10:00:00+08:00",
            uploads=["0" * 32],
        )
    assert svc.store.by_author("h") == []


def test_expired_uploads_are_rejected_and_their_files_removed(svc):
    author = v(svc, AUTHOR)
    upload = svc.stage_photo(shot(), author)
    path = svc.store.photo_dir / f"{upload['id']}.png"
    assert path.exists()
    old = (now_beijing() - timedelta(hours=2)).isoformat(timespec="seconds")
    con = svc.store._connect()
    with con:
        con.execute("UPDATE photo_uploads SET created_at=?", (old,))
    con.close()
    with pytest.raises(MarkingError) as info:
        svc.create(closure(photos=[upload["id"]]), author)
    assert info.value.code == "photo_expired"
    assert not path.exists() and svc.store.uploads([upload["id"]]) == {}


def test_pending_uploads_per_device_are_capped(svc):
    author = v(svc, AUTHOR)
    for _ in range(service_mod.MAX_PENDING_UPLOADS):
        svc.stage_photo(shot(), author)
    with pytest.raises(MarkingError) as info:
        svc.stage_photo(shot(), author)
    assert info.value.code == "upload_pending"
    leftover = {p.name for p in svc.store.photo_dir.iterdir()}
    assert len(leftover) == service_mod.MAX_PENDING_UPLOADS  # 被拒的那张没有留下文件


def test_author_keeps_at_least_one_photo_but_witnesses_can_remove_theirs(svc):
    out = create(svc, closure())
    mid, token = out["marking"]["id"], out["edit_token"]
    first = svc.store.photos(mid)[0]["id"]
    with pytest.raises(MarkingError) as info:
        svc.delete_photo(first, token, v(svc, AUTHOR))
    assert info.value.code == "photo_last"
    second = svc.add_photo(mid, jpeg(), token, v(svc, AUTHOR))
    svc.delete_photo(first, token, v(svc, AUTHOR))  # 补了一张新的，就可以删旧的
    with pytest.raises(MarkingError) as info:
        svc.delete_photo(second["id"], None, v(svc, AUTHOR))
    assert info.value.code == "photo_last"

    # 管理员隐藏了作者的照片后，只剩其他人补的一张：他删自己的照片不受限制
    witness = svc.add_photo(mid, png(), None, v(svc, OTHER))
    svc.set_photo_visibility(second["id"], True, "拍到了车牌号", v(svc, ADMIN_DEVICE))
    svc.delete_photo(witness["id"], None, v(svc, OTHER))
    assert svc.public(mid, v(svc, AUTHOR))["photo_count"] == 0


def test_bad_photo_is_rejected_with_a_readable_message(svc):
    out = create(svc, closure())
    with pytest.raises(MarkingError) as info:
        svc.add_photo(out["marking"]["id"], b"not an image", out["edit_token"], v(svc, AUTHOR))
    assert info.value.code == "bad_photo" and "JPEG" in info.value.message
    with pytest.raises(MarkingError) as info:
        svc.stage_photo(b"not an image", v(svc, AUTHOR))
    assert info.value.code == "bad_photo"


# ---------- 管理员 ----------


def _review(svc, mid, device=ADMIN_DEVICE, **kw):
    body = {
        "decision": "verify",
        "version": svc.store.get(mid)["version"],
        "note": "现场照片清楚，路口确有围挡",
        "basis": ["photo", "site_visit"],
        "checks": {"location": True, "type": True, "current": True},
        "photos_reviewed": [p["id"] for p in svc.store.photos(mid)],
        **kw,
    }
    return svc.admin_review(mid, body, v(svc, device))


@pytest.fixture()
def quick_review(monkeypatch):
    monkeypatch.setattr(service_mod, "REVIEW_MIN_SECONDS", 0.0)


def test_admin_is_disabled_without_token_and_locks_out_guessing(tmp_path):
    off = make_service(tmp_path / "off", admin="")
    with pytest.raises(MarkingError) as info:
        off.require_admin("anything", v(off))
    assert info.value.status == 503
    on = make_service(tmp_path / "on")
    for _ in range(10):
        with pytest.raises(MarkingError) as info:
            on.require_admin("guess", v(on))
        assert info.value.status == 401
    with pytest.raises(MarkingError) as info:
        on.require_admin(ADMIN, v(on))  # 输错太多次后，即使口令对也要等
    assert info.value.status == 429


def test_verify_requires_opening_the_detail_and_waiting(svc):
    out = create(svc, closure())
    mid = out["marking"]["id"]
    svc.add_photo(mid, jpeg(), out["edit_token"], v(svc, AUTHOR))
    with pytest.raises(MarkingError) as info:
        _review(svc, mid)
    assert info.value.code == "review_not_opened"
    svc.admin_detail(mid, v(svc, THIRD))
    with pytest.raises(MarkingError) as info:
        _review(svc, mid)  # 别的设备打开过，不算提交核实的这台看过
    assert info.value.code == "review_not_opened"
    with pytest.raises(MarkingError) as info:
        _review(svc, mid, device=None)
    assert info.value.code == "missing_device"
    svc.admin_detail(mid, v(svc, ADMIN_DEVICE))
    with pytest.raises(MarkingError) as info:
        _review(svc, mid)  # 默认至少停留 5 秒
    assert info.value.code == "review_too_fast"


def test_verify_checks_every_rule(svc, quick_review):
    out = create(svc, closure())
    mid, token = out["marking"]["id"], out["edit_token"]
    p1 = svc.add_photo(mid, jpeg(), token, v(svc, AUTHOR))
    svc.add_photo(mid, png(), None, v(svc, OTHER))
    detail = svc.admin_detail(mid, v(svc, ADMIN_DEVICE))
    assert detail["author_history"]["total"] == 1 and len(detail["photos"]) == 3

    with pytest.raises(MarkingError, match="逐项确认"):
        _review(svc, mid, checks={"location": True, "type": True})
    with pytest.raises(MarkingError, match="至少写"):
        _review(svc, mid, note="可以")
    with pytest.raises(MarkingError) as info:
        _review(svc, mid, photos_reviewed=[p1["id"]])
    assert info.value.code == "photos_unseen"
    with pytest.raises(MarkingError, match="多人确认"):
        _review(svc, mid, basis=["multi_confirm"])
    with pytest.raises(MarkingError) as info:
        _review(svc, mid, device=AUTHOR)
    assert info.value.code == "own_marking"

    done = _review(svc, mid)
    assert done["status"] == "verified" and done["review"]["decision"] == "verify"
    assert done["review"]["basis"] == ["现场照片", "实地走访"]
    public = svc.detail(mid, v(svc, THIRD))
    assert public["status"] == "verified" and public["review"]["note"].startswith("现场照片")


def test_edit_after_opening_invalidates_the_review(svc, quick_review):
    out = create(svc, closure())
    mid, token = out["marking"]["id"], out["edit_token"]
    svc.admin_detail(mid, v(svc, ADMIN_DEVICE))
    svc.update(mid, {"radius_m": 90}, 1, token, v(svc, AUTHOR))
    with pytest.raises(MarkingError) as info:
        _review(svc, mid, basis=["site_visit"], version=1)
    assert info.value.code == "version_conflict"
    with pytest.raises(MarkingError) as info:
        _review(svc, mid, basis=["site_visit"])
    assert info.value.code == "review_stale"


def test_reject_archive_reopen_and_queue(svc, quick_review):
    a = create(svc, closure())["marking"]["id"]
    b = create(svc, closure(lat=north(500)))["marking"]["id"]
    svc.vote(b, -1, v(svc, OTHER))
    svc.vote(b, -1, v(svc, THIRD))
    queue = svc.admin_queue("pending", v(svc, ADMIN_DEVICE))
    assert [m["id"] for m in queue["items"]] == [b, a]  # 有争议的排前面
    assert queue["counts"]["pending"] == 2 and queue["counts"]["disputed"] == 1

    with pytest.raises(MarkingError, match="驳回原因"):
        _review(svc, a, decision="reject")
    rejected = _review(svc, a, decision="reject", reason="duplicate")
    assert rejected["status"] == "rejected"
    reopened = _review(svc, a, decision="reopen")
    assert reopened["status"] == "pending"
    archived = _review(svc, b, decision="archive", note="围挡已拆除，现场核实")
    assert archived["status"] == "archived"
    counts = svc.admin_queue("archived", v(svc, ADMIN_DEVICE))["counts"]
    assert counts["archived"] == 1 and counts["pending"] == 1


def test_hidden_photo_is_not_served_publicly(svc):
    out = create(svc, closure())
    photo = svc.add_photo(out["marking"]["id"], jpeg(), out["edit_token"], v(svc, AUTHOR))
    svc.set_photo_visibility(photo["id"], True, "拍到了行人正脸", v(svc, ADMIN_DEVICE))
    with pytest.raises(MarkingError):
        svc.photo_path(photo["id"])
    assert svc.photo_path(photo["id"], admin=True)[0].exists()
    shown = [p["id"] for p in svc.detail(out["marking"]["id"], v(svc, OTHER))["photos"]]
    assert photo["id"] not in shown and len(shown) == 1


def test_for_analysis_marks_own_markings(svc):
    create(svc, closure())
    create(
        svc,
        {
            "type": "facility_extra",
            "lat": north(300),
            "lng": LNG0,
            "category": "医药",
            "name": "村卫生室",
        },
        OTHER,
    )
    own_hash = v(svc, AUTHOR).device_hash
    items = svc.for_analysis(LAT0, LNG0, 2500, own_hash)
    assert {m["type"]: m["mine"] for m in items} == {"closure": True, "facility_extra": False}
    assert all(math.isfinite(m["distance_m"]) for m in items)
