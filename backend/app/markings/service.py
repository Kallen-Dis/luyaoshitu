"""标注的业务规则：谁能做什么、什么状态下能做、做完留下什么记录。

所有失败都抛 MarkingError(HTTP 状态码, 代码, 给用户看的中文说明)，路由层原样返回。

状态：pending 待核实 → verified 已核实（只有管理员审核能做到）；rejected 已驳回、
retracted 已撤回、archived 已归档；过了有效期的待核实 / 已核实在读取时显示为 expired。

现场照片必填：新建前先传照片（stage_photo），新建时引用预传 ID，在同一个事务里挂上；
作者不能删掉最后一张可见照片。

管理员核实刻意做成「不能一键」：必须先打开详情（服务端记下打开时间与版本），
停留一段时间后才能提交；要逐项确认位置、类型、时效，选择核实依据并写审核意见；
依据里选了现场照片，就必须看过每一张；不能核实自己设备提交的标注。
"""

from __future__ import annotations

import contextlib
import hmac
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import Settings
from ..isochrone.geometry import METERS_PER_DEG_LAT, haversine_m, meters_per_deg_lng
from ..poi.collect import normalize_name
from . import models, trust
from .models import MarkingValidationError, now_beijing
from .photos import EXTENSIONS, MAX_BYTES, CleanImage, PhotoError, sanitize
from .store import MarkingStore, PhotoLimit, StatusConflict, UploadMissing, VersionConflict

MAX_PHOTOS = 6
MAX_PHOTOS_PER_UPLOADER = 3
# 现场照片必填：新建时至少附这么多张（先传到 /api/markings/uploads，新建时一起挂上）
MIN_PHOTOS = 1
# 预传照片多久内要挂上标注，过期的连文件一起清掉
UPLOAD_TTL_S = 3600.0
# 每台设备同时挂着、还没用上的预传照片上限
MAX_PENDING_UPLOADS = 12
DUPLICATE_M = 30.0
FACILITY_MATCH_M = 50.0
NEARBY_MAX_M = 5000.0
REVIEW_MIN_SECONDS = 5.0
REVIEW_TICKET_TTL_S = 2 * 3600.0
REVIEW_NOTE_MIN = 6
REVIEW_NOTE_MAX = 200

REVIEW_BASIS: dict[str, str] = {
    "photo": "现场照片",
    "site_visit": "实地走访",
    "official": "官方信息",
    "multi_confirm": "多人确认",
}
REJECT_REASONS: dict[str, str] = {
    "inaccurate": "位置或内容不准确",
    "duplicate": "重复标注",
    "not_current": "现场已经不是这样",
    "inappropriate": "内容不当",
    "other": "其他原因",
}
STATUS_LABELS: dict[str, str] = {
    "pending": "待核实",
    "verified": "已核实",
    "rejected": "已驳回",
    "retracted": "已撤回",
    "archived": "已归档",
    "expired": "已过期",
}
REVIEW_CHECKS = ("location", "type", "current")
ADMIN_QUEUES = ("pending", "disputed", "expired", "verified", "rejected", "archived", "retracted")

EVENT_LABELS: dict[str, str] = {
    "create": "提交标注",
    "edit": "修改内容",
    "revert": "恢复到旧版本",
    "retract": "撤回",
    "restore": "恢复",
    "renew": "续期",
    "photo_add": "补充照片",
    "photo_delete": "删除照片",
    "photo_hide": "隐藏照片",
    "photo_show": "恢复显示照片",
    "verify": "核实",
    "reject": "驳回",
    "archive": "归档",
    "reopen": "重新打开",
}
PHOTO_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class MarkingError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def detail(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.extra}


@dataclass(frozen=True)
class Viewer:
    """一次请求的发起方。device_hash 为 None 表示没带（或带了不合法的）设备 ID。"""

    device_hash: str | None
    ip: str


def iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _invalid(exc: MarkingValidationError) -> MarkingError:
    return MarkingError(400, "invalid", str(exc), field=exc.field)


def _status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


class MarkingService:
    def __init__(self, store: MarkingStore, settings: Settings) -> None:
        self.store = store
        self.admin_token = settings.admin_token if settings.admin_enabled else ""
        self._salt = settings.marking_salt or None
        self.limiter = trust.RateLimiter()
        # 管理员打开详情的记录：(标注 ID, 管理员设备) → (打开时刻, 当时的版本)。
        # 按设备分开记：别的设备打开过，不代表提交核实的这台看过
        self._tickets: dict[tuple[int, str | None], tuple[float, int]] = {}
        self._lock = threading.Lock()

    # ---------- 身份 ----------

    def salt(self) -> str:
        if self._salt:
            return self._salt
        with self._lock:
            if not self._salt:
                self.store.init()
                path = self.store.root / "salt"
                if path.exists():
                    value = path.read_text(encoding="utf-8").strip()
                else:
                    value = secrets.token_hex(16)
                    path.write_text(value, encoding="utf-8")
                self._salt = value
        return self._salt

    def viewer(self, device_id: str | None, ip: str | None) -> Viewer:
        device_hash = None
        if device_id and trust.DEVICE_RE.match(device_id):
            device_hash = trust.hash_value(device_id, self.salt())
        return Viewer(device_hash, ip or "unknown")

    @staticmethod
    def _device(viewer: Viewer) -> str:
        if not viewer.device_hash:
            raise MarkingError(400, "missing_device", "缺少设备标识，请刷新页面后重试")
        return viewer.device_hash

    def _limit(self, action: str, viewer: Viewer) -> None:
        per_device, per_ip = trust.LIMITS[action]
        try:
            if viewer.device_hash:
                self.limiter.hit(action, f"d:{viewer.device_hash}", per_device, 3600)
            self.limiter.hit(action, f"i:{viewer.ip}", per_ip, 3600)
        except trust.RateLimited as exc:
            minutes = max(1, round(exc.retry_after_s / 60))
            raise MarkingError(
                429,
                "rate_limited",
                f"操作太频繁，请约 {minutes} 分钟后再试",
                retry_after_s=exc.retry_after_s,
            ) from exc

    def _row(self, marking_id: int) -> dict[str, Any]:
        row = self.store.get(marking_id)
        if row is None:
            raise MarkingError(404, "not_found", "这条标注不存在")
        return row

    @staticmethod
    def _authorize(row: dict[str, Any], token: str | None) -> None:
        if not trust.token_matches(token, row["edit_hash"]):
            raise MarkingError(
                403,
                "forbidden",
                "只有提交这条标注的浏览器才能修改它（编辑凭据缺失或不匹配）",
            )

    # ---------- 序列化 ----------

    def _review_of(self, event: dict[str, Any] | None) -> dict[str, Any] | None:
        if event is None:
            return None
        payload = event.get("payload") or {}
        return {
            "decision": event["action"],
            "basis": [REVIEW_BASIS.get(b, b) for b in payload.get("basis") or []],
            "reason": REJECT_REASONS.get(payload.get("reason") or "", None),
            "note": payload.get("note") or "",
            "at": event["created_at"],
        }

    def publics(
        self,
        rows: list[dict[str, Any]],
        viewer: Viewer,
        now: datetime,
        *,
        origin: tuple[float, float] | None = None,
    ) -> list[dict[str, Any]]:
        ids = [int(r["id"]) for r in rows]
        photos = self.store.photo_counts(ids)
        votes = self.store.votes_of(ids, viewer.device_hash) if viewer.device_hash else {}
        reviews = self.store.last_events(ids, ("verify", "reject", "archive", "reopen"))
        out = []
        for row in rows:
            mid = int(row["id"])
            spec = row["spec"]
            item: dict[str, Any] = {
                "id": mid,
                "type": row["type"],
                "title": models.title(spec),
                "status": trust.effective_status(row, now),
                "spec": spec,
                "geometry": row["geometry"],
                "lat": row["lat"],
                "lng": row["lng"],
                "source": row["source"],
                "version": int(row["version"]),
                "confirms": int(row["confirms"]),
                "disputes": int(row["disputes"]),
                "disputed": trust.is_disputed(row),
                "confidence": trust.confidence(row, now),
                "my_vote": votes.get(mid, 0),
                "photo_count": photos.get(mid, 0),
                "mine": bool(viewer.device_hash) and viewer.device_hash == row["author_hash"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "expires_at": row["expires_at"],
                "review": self._review_of(reviews.get(mid)),
            }
            if origin is not None:
                item["distance_m"] = round(models.distance_to(spec, *origin))
            out.append(item)
        return out

    def public(self, marking_id: int, viewer: Viewer) -> dict[str, Any]:
        return self.publics([self._row(marking_id)], viewer, now_beijing())[0]

    @staticmethod
    def _photo_public(photo: dict[str, Any], viewer: Viewer) -> dict[str, Any]:
        return {
            "id": photo["id"],
            "url": f"/api/markings/photos/{photo['id']}",
            "width": photo["width"],
            "height": photo["height"],
            "role": photo["role"],
            "status": photo["status"],
            "mine": bool(viewer.device_hash) and viewer.device_hash == photo["uploader_hash"],
            "created_at": photo["created_at"],
        }

    def _timeline(
        self, row: dict[str, Any], events: list[dict[str, Any]], *, admin: bool
    ) -> list[dict[str, Any]]:
        out = []
        for e in events:
            action = e["action"]
            if action in ("confirm", "dispute", "unvote") and not admin:
                continue  # 公开时间线只列计数，不暴露逐票记录
            payload = e.get("payload") or {}
            actor = e["actor"]
            same_device = e.get("actor_hash") == row["author_hash"]
            if actor == "author" or (actor == "voter" and same_device):
                who = "author"
            elif actor == "admin":
                who = "admin"
            elif actor == "system":
                who = "system"
            else:
                who = "other"
            label = EVENT_LABELS.get(action) or {
                "confirm": "确认属实",
                "dispute": "提出异议",
                "unvote": "撤回投票",
            }.get(action, action)
            detail = ""
            if action == "edit" and payload.get("unverified"):
                detail = "内容变了，需要重新核实"
            elif action == "revert":
                detail = f"回到第 {payload.get('to_version')} 版"
            elif action in ("verify", "reject", "archive", "reopen"):
                parts = [REVIEW_BASIS.get(b, b) for b in payload.get("basis") or []]
                if payload.get("reason"):
                    parts.append(REJECT_REASONS.get(payload["reason"], payload["reason"]))
                detail = "、".join(parts)
                if payload.get("note"):
                    detail = f"{detail}：{payload['note']}" if detail else payload["note"]
            elif action == "renew":
                detail = f"有效期至 {str(payload.get('expires_at', ''))[:10]}"
            out.append(
                {
                    "action": action,
                    "label": label,
                    "actor": who,
                    "detail": detail,
                    "version": payload.get("version"),
                    "at": e["created_at"],
                }
            )
        return out

    # ---------- 查询 ----------

    def _active_near(
        self, lat: float, lng: float, radius_m: float, now: datetime
    ) -> list[tuple[float, dict[str, Any]]]:
        d_lat = radius_m / METERS_PER_DEG_LAT
        d_lng = radius_m / meters_per_deg_lng(lat)
        rows = self.store.in_bbox(lat - d_lat, lat + d_lat, lng - d_lng, lng + d_lng, trust.ACTIVE)
        out = []
        for row in rows:
            if trust.effective_status(row, now) not in trust.ACTIVE:
                continue
            dist = models.distance_to(row["spec"], lat, lng)
            if dist <= radius_m:
                out.append((dist, row))
        return out

    def nearby(
        self,
        lat: float,
        lng: float,
        radius_m: float,
        viewer: Viewer,
        types: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        try:
            lat, lng = models.check_point(lat, lng, "查询中心")
        except MarkingValidationError as exc:
            raise _invalid(exc) from exc
        if not 0 < radius_m <= NEARBY_MAX_M:
            raise MarkingError(400, "invalid", f"查询半径应在 0~{NEARBY_MAX_M:.0f} 米之间")
        now = now_beijing()
        found = [
            (d, r)
            for d, r in self._active_near(lat, lng, radius_m, now)
            if types is None or r["type"] in types
        ]
        items = self.publics([r for _, r in found], viewer, now, origin=(lat, lng))
        items.sort(
            key=lambda m: (
                m["status"] != "verified",
                not m["mine"],
                -m["confidence"],
                m.get("distance_m", 0),
            )
        )
        return items

    def mine(self, viewer: Viewer) -> list[dict[str, Any]]:
        device = self._device(viewer)
        return self.publics(self.store.by_author(device), viewer, now_beijing())

    def detail(self, marking_id: int, viewer: Viewer) -> dict[str, Any]:
        row = self._row(marking_id)
        now = now_beijing()
        item = self.publics([row], viewer, now)[0]
        if row["status"] in ("retracted", "rejected") and not item["mine"]:
            # 撤回是作者的决定，驳回是审核结论，都不再对其他人展示
            raise MarkingError(404, "not_found", "这条标注不存在或已撤回")
        item["photos"] = [self._photo_public(p, viewer) for p in self.store.photos(marking_id)]
        item["events"] = self._timeline(row, self.store.events(marking_id), admin=False)
        item["versions"] = [
            {"version": v["version"], "title": models.title(v["spec"]), "at": v["created_at"]}
            for v in self.store.versions(marking_id)
        ]
        return item

    # ---------- 新建 ----------

    def _duplicate(
        self, spec: dict[str, Any], columns: dict[str, Any], now: datetime
    ) -> dict[str, Any] | None:
        lat, lng = columns["lat"], columns["lng"]
        for _, row in self._active_near(lat, lng, 80.0, now):
            if row["type"] != spec["type"]:
                continue
            other = row["spec"]
            d = haversine_m(lat, lng, row["lat"], row["lng"])
            if spec["type"] == "closure" and d <= DUPLICATE_M:
                return row
            if (
                spec["type"] == "facility_missing"
                and other["category"] == spec["category"]
                and normalize_name(other["name"]) == normalize_name(spec["name"])
                and d <= FACILITY_MATCH_M
            ):
                return row
            if (
                spec["type"] == "facility_extra"
                and other["category"] == spec["category"]
                and d <= DUPLICATE_M
            ):
                return row
            if (
                spec["type"] == "gray_area"
                and d <= DUPLICATE_M
                and set(other["categories"]) & set(spec["categories"])
            ):
                return row
        return None

    def _upload_ids(self, raw: Any) -> list[str]:
        """新建时引用的预传照片 ID：必填、去重、格式合法、数量在范围内。"""
        if raw is None:
            raw = []
        if not isinstance(raw, list | tuple) or not all(isinstance(x, str) for x in raw):
            raise MarkingError(400, "invalid", "照片参数格式不对", field="photos")
        ids = list(raw)
        if len(ids) < MIN_PHOTOS:
            raise MarkingError(
                400,
                "photo_required",
                "请至少附一张现场照片：管理员要靠它核实，没有照片的标注无法审核",
                field="photos",
            )
        if len(ids) > MAX_PHOTOS_PER_UPLOADER:
            raise MarkingError(
                400, "invalid", f"新建时最多附 {MAX_PHOTOS_PER_UPLOADER} 张照片", field="photos"
            )
        if len(set(ids)) != len(ids) or not all(PHOTO_ID_RE.match(x) for x in ids):
            raise MarkingError(400, "invalid", "照片参数格式不对", field="photos")
        return ids

    @staticmethod
    def _uploads_expired(missing: list[str]) -> MarkingError:
        return MarkingError(
            409,
            "photo_expired",
            "有照片的上传已经过期或找不到了，请重新添加照片后再提交",
            field="photos",
            missing=missing,
        )

    def _cleanup_uploads(self, now: datetime) -> None:
        """清掉过期没用上的预传照片（库里的行和磁盘上的文件）。"""
        before = iso(now - timedelta(seconds=UPLOAD_TTL_S))
        for row in self.store.take_expired_uploads(before):
            ext = EXTENSIONS.get(str(row["mime"]))
            if ext:
                with contextlib.suppress(OSError):
                    (self.store.photo_dir / f"{row['id']}.{ext}").unlink(missing_ok=True)

    def stage_photo(self, data: bytes, viewer: Viewer) -> dict[str, Any]:
        """新建标注前先传照片：清洗、存盘，返回预传 ID，新建时一起提交。"""
        device = self._device(viewer)
        self._limit("photo", viewer)
        try:
            image = sanitize(data)
        except PhotoError as exc:
            raise MarkingError(400, "bad_photo", str(exc)) from exc
        now = now_beijing()
        self._cleanup_uploads(now)
        upload_id = secrets.token_hex(16)
        path = self._write_photo(upload_id, image)
        try:
            self.store.stage_upload(
                {
                    "id": upload_id,
                    "uploader_hash": device,
                    "mime": image.mime,
                    "bytes": len(image.data),
                    "width": image.width,
                    "height": image.height,
                    "sha256": image.sha256,
                },
                now=iso(now),
                max_pending=MAX_PENDING_UPLOADS,
            )
        except PhotoLimit as exc:
            path.unlink(missing_ok=True)
            raise MarkingError(
                429,
                "upload_pending",
                "传了太多还没用上的照片，请先提交标注，或一小时后再试",
            ) from exc
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return {
            "id": upload_id,
            "width": image.width,
            "height": image.height,
            "expires_at": iso(now + timedelta(seconds=UPLOAD_TTL_S)),
        }

    def _write_photo(self, photo_id: str, image: CleanImage) -> Path:
        """先写临时文件再改名：写到一半断电也不会留下半张图。"""
        self.store.init()
        path = self.store.photo_dir / f"{photo_id}.{image.ext}"
        tmp = path.with_suffix(".part")
        tmp.write_bytes(image.data)
        tmp.replace(path)
        return path

    def create(self, payload: dict[str, Any], viewer: Viewer) -> dict[str, Any]:
        device = self._device(viewer)
        source = payload.get("source") or "user"
        if source not in models.SOURCES:
            raise MarkingError(400, "invalid", "未知的标注来源")
        try:
            spec, columns = models.normalize(payload)
            days = models.ttl_days(spec, payload.get("expires_in_days"))
        except MarkingValidationError as exc:
            raise _invalid(exc) from exc
        upload_ids = self._upload_ids(payload.get("photos"))
        now = now_beijing()
        self._cleanup_uploads(now)
        since = iso(now - timedelta(seconds=UPLOAD_TTL_S))
        # 先查一遍给出清楚的提示；真正的检查在写库的事务里再做一次
        staged = self.store.uploads(upload_ids)
        missing = [
            u
            for u in upload_ids
            if u not in staged
            or staged[u]["uploader_hash"] != device
            or staged[u]["created_at"] < since
        ]
        if missing:
            raise self._uploads_expired(missing)
        if len({staged[u]["sha256"] for u in upload_ids}) != len(upload_ids):
            raise MarkingError(
                400, "photo_duplicate", "这几张照片里有重复的，请换一张", field="photos"
            )
        if not payload.get("force"):
            dup = self._duplicate(spec, columns, now)
            if dup is not None:
                raise MarkingError(
                    409,
                    "duplicate",
                    "附近已有一条同类标注。如果说的是同一处，确认它比再建一条更有用。",
                    existing=self.publics([dup], viewer, now)[0],
                )
        self._limit("create", viewer)
        token, token_hash = trust.new_edit_token()
        try:
            marking_id = self.store.insert(
                spec,
                columns,
                source=source,
                author_hash=device,
                edit_hash=token_hash,
                now=iso(now),
                expires_at=iso(models.expires_at(now, days)),
                uploads=upload_ids,
                uploads_since=since,
            )
        except UploadMissing as exc:
            # 两个页面同时用同一批照片提交：先到的挂上了，后到的照片已经不在预传区
            raise self._uploads_expired(exc.upload_ids) from exc
        except PhotoLimit as exc:
            raise MarkingError(
                400, "photo_duplicate", "这几张照片里有重复的，请换一张", field="photos"
            ) from exc
        return {"marking": self.public(marking_id, viewer), "edit_token": token}

    # ---------- 作者操作 ----------

    def update(
        self,
        marking_id: int,
        patch: dict[str, Any],
        expected_version: int,
        token: str | None,
        viewer: Viewer,
    ) -> dict[str, Any]:
        row = self._row(marking_id)
        self._authorize(row, token)
        self._limit("edit", viewer)
        if row["status"] not in trust.ACTIVE:
            hint = "请先恢复再修改" if row["status"] == "retracted" else "请新建一条"
            raise MarkingError(
                409, "bad_status", f"这条标注{_status_label(row['status'])}，不能修改，{hint}"
            )
        if int(row["version"]) != expected_version:
            raise self._conflict(int(row["version"]))
        patch = dict(patch)
        renew = patch.pop("expires_in_days", None)
        content = {k: v for k, v in patch.items() if v is not None}
        spec = row["spec"]
        columns: dict[str, Any] | None = None
        try:
            if content:
                spec, columns = models.normalize(models.merge_patch(row["spec"], content))
            days = models.ttl_days(spec, renew) if renew is not None else None
        except MarkingValidationError as exc:
            raise _invalid(exc) from exc
        changed = [k for k in spec if spec.get(k) != row["spec"].get(k)]
        if not changed and days is None:
            raise MarkingError(400, "no_change", "没有需要保存的修改")
        now = now_beijing()
        version = expected_version
        try:
            if changed and columns is not None:
                was_verified = row["status"] == "verified"
                version = self.store.update_spec(
                    marking_id,
                    expected_version=expected_version,
                    spec=spec,
                    columns=columns,
                    status="pending",
                    now=iso(now),
                    actor="author",
                    actor_hash=viewer.device_hash,
                    action="edit",
                    payload={"fields": changed, "unverified": was_verified},
                )
            if days is not None:
                self.store.set_expiry(
                    marking_id,
                    expected_version=version,
                    expires_at=iso(models.expires_at(now, days)),
                    now=iso(now),
                    actor="author",
                    actor_hash=viewer.device_hash,
                )
        except VersionConflict as exc:
            raise self._conflict(exc.current) from exc
        return self.public(marking_id, viewer)

    @staticmethod
    def _conflict(current: int) -> MarkingError:
        return MarkingError(
            409,
            "version_conflict",
            f"这条标注刚被改过（现在是第 {current} 版），请刷新后再操作",
            current_version=current,
        )

    def retract(self, marking_id: int, token: str | None, viewer: Viewer) -> dict[str, Any]:
        row = self._row(marking_id)
        self._authorize(row, token)
        try:
            self.store.transition(
                marking_id,
                from_statuses=trust.ACTIVE,
                to_status="retracted",
                now=iso(now_beijing()),
                actor="author",
                actor_hash=viewer.device_hash,
                action="retract",
                payload={"previous": row["status"], "version": int(row["version"])},
            )
        except StatusConflict as exc:
            raise MarkingError(
                409, "bad_status", f"这条标注{_status_label(exc.current)}，不能撤回"
            ) from exc
        return self.public(marking_id, viewer)

    def restore(self, marking_id: int, token: str | None, viewer: Viewer) -> dict[str, Any]:
        row = self._row(marking_id)
        self._authorize(row, token)
        if row["status"] != "retracted":
            raise MarkingError(
                409, "bad_status", f"这条标注{_status_label(row['status'])}，不需要恢复"
            )
        last = self.store.last_events([marking_id], ("retract",)).get(marking_id)
        payload = (last or {}).get("payload") or {}
        # 撤回前已核实、且之后内容没变：核实结论仍然成立，恢复成已核实；否则回到待核实
        to_status = (
            "verified"
            if payload.get("previous") == "verified"
            and payload.get("version") == int(row["version"])
            else "pending"
        )
        try:
            self.store.transition(
                marking_id,
                from_statuses=("retracted",),
                to_status=to_status,
                now=iso(now_beijing()),
                actor="author",
                actor_hash=viewer.device_hash,
                action="restore",
            )
        except StatusConflict as exc:
            raise MarkingError(
                409, "bad_status", f"这条标注{_status_label(exc.current)}，不需要恢复"
            ) from exc
        return self.public(marking_id, viewer)

    def revert(
        self,
        marking_id: int,
        to_version: int,
        expected_version: int,
        token: str | None,
        viewer: Viewer,
    ) -> dict[str, Any]:
        row = self._row(marking_id)
        self._authorize(row, token)
        if row["status"] not in trust.ACTIVE:
            raise MarkingError(
                409, "bad_status", f"这条标注{_status_label(row['status'])}，不能回退"
            )
        if int(row["version"]) != expected_version:
            raise self._conflict(int(row["version"]))
        if not 1 <= to_version < expected_version:
            raise MarkingError(400, "invalid", "只能回到比当前更早的版本")
        old = self.store.version_spec(marking_id, to_version)
        if old is None:
            raise MarkingError(404, "not_found", f"没有第 {to_version} 版")
        try:
            spec, columns = models.normalize(old)
        except MarkingValidationError as exc:
            raise MarkingError(
                409, "invalid", f"第 {to_version} 版已不符合现在的校验规则：{exc}"
            ) from exc
        if spec == row["spec"]:
            raise MarkingError(400, "no_change", "那一版的内容与现在相同")
        try:
            self.store.update_spec(
                marking_id,
                expected_version=expected_version,
                spec=spec,
                columns=columns,
                status="pending",
                now=iso(now_beijing()),
                actor="author",
                actor_hash=viewer.device_hash,
                action="revert",
                payload={"to_version": to_version, "unverified": row["status"] == "verified"},
            )
        except VersionConflict as exc:
            raise self._conflict(exc.current) from exc
        return self.public(marking_id, viewer)

    # ---------- 投票 ----------

    def vote(self, marking_id: int, vote: int, viewer: Viewer) -> dict[str, Any]:
        device = self._device(viewer)
        if vote not in (-1, 0, 1):
            raise MarkingError(400, "invalid", "投票只能是确认、有异议或撤票")
        row = self._row(marking_id)
        if row["author_hash"] == device:
            raise MarkingError(403, "own_marking", "不能给自己提交的标注投票")
        status = trust.effective_status(row, now_beijing())
        if status not in trust.ACTIVE:
            raise MarkingError(409, "bad_status", f"这条标注{_status_label(status)}，不能再投票")
        self._limit("vote", viewer)
        self.store.set_vote(marking_id, device, vote, iso(now_beijing()))
        return self.public(marking_id, viewer)

    # ---------- 照片 ----------

    def add_photo(
        self, marking_id: int, data: bytes, token: str | None, viewer: Viewer
    ) -> dict[str, Any]:
        device = self._device(viewer)
        row = self._row(marking_id)
        status = trust.effective_status(row, now_beijing())
        if status not in trust.ACTIVE:
            raise MarkingError(
                409, "bad_status", f"这条标注{_status_label(status)}，不能再补充照片"
            )
        if token is not None and not trust.token_matches(token, row["edit_hash"]):
            raise MarkingError(403, "forbidden", "编辑凭据不匹配")
        role = "author" if token is not None or device == row["author_hash"] else "witness"
        self._limit("photo", viewer)
        try:
            image = sanitize(data)
        except PhotoError as exc:
            raise MarkingError(400, "bad_photo", str(exc)) from exc
        photo_id = secrets.token_hex(16)
        path = self._write_photo(photo_id, image)
        try:
            self.store.add_photo(
                {
                    "id": photo_id,
                    "marking_id": marking_id,
                    "uploader_hash": device,
                    "role": role,
                    "mime": image.mime,
                    "bytes": len(image.data),
                    "width": image.width,
                    "height": image.height,
                    "sha256": image.sha256,
                },
                actor="author" if role == "author" else "voter",
                now=iso(now_beijing()),
                max_total=MAX_PHOTOS,
                max_per_uploader=MAX_PHOTOS_PER_UPLOADER,
            )
        except PhotoLimit as exc:
            path.unlink(missing_ok=True)
            message = {
                "duplicate": "这张照片已经上传过了",
                "total": f"每条标注最多 {MAX_PHOTOS} 张照片",
                "uploader": f"你已经为这条标注上传了 {MAX_PHOTOS_PER_UPLOADER} 张照片",
            }[exc.kind]
            raise MarkingError(409, f"photo_{exc.kind}", message) from exc
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        photo = self.store.photo(photo_id)
        assert photo is not None
        return self._photo_public(photo, viewer)

    def _photo_row(self, photo_id: str) -> dict[str, Any]:
        if not PHOTO_ID_RE.match(photo_id or ""):
            raise MarkingError(404, "not_found", "照片不存在")
        photo = self.store.photo(photo_id)
        if photo is None or photo["status"] == "deleted":
            raise MarkingError(404, "not_found", "照片不存在")
        return photo

    def photo_path(self, photo_id: str, *, admin: bool = False) -> tuple[Path, str]:
        photo = self._photo_row(photo_id)
        if photo["status"] != "visible" and not admin:
            raise MarkingError(404, "not_found", "照片不存在")
        if not admin:
            row = self.store.get(int(photo["marking_id"]))
            if row is None or row["status"] in ("retracted", "rejected"):
                raise MarkingError(404, "not_found", "照片不存在")
        path, mime = self._file_of(photo)
        if not path.exists():
            raise MarkingError(404, "not_found", "照片文件已丢失")
        return path, mime

    def delete_photo(self, photo_id: str, token: str | None, viewer: Viewer) -> None:
        photo = self._photo_row(photo_id)
        row = self._row(int(photo["marking_id"]))
        own = bool(viewer.device_hash) and viewer.device_hash == photo["uploader_hash"]
        has_token = trust.token_matches(token, row["edit_hash"])
        if not own and not has_token:
            raise MarkingError(403, "forbidden", "只能删除自己上传的照片")
        # 作者那边，照片是必填的：不能把最后一张可见照片删掉（可以先补一张，或撤回整条）。
        # 其他用户删自己补充的照片不受限制——照片可能拍到了人，删除的权利不能被绑住
        acting_as_author = has_token or (
            bool(viewer.device_hash) and viewer.device_hash == row["author_hash"]
        )
        try:
            self.store.set_photo_status(
                photo_id,
                from_statuses=("visible", "hidden"),
                to_status="deleted",
                now=iso(now_beijing()),
                actor="author" if row["author_hash"] == viewer.device_hash else "voter",
                actor_hash=viewer.device_hash,
                keep_one_visible=acting_as_author,
            )
        except StatusConflict as exc:
            raise MarkingError(404, "not_found", "照片不存在") from exc
        except PhotoLimit as exc:
            raise MarkingError(
                409,
                "photo_last",
                "现场照片是必填的，这是最后一张了。可以先补一张新的再删，或者撤回整条标注",
            ) from exc
        path, _ = self._file_of(photo)
        path.unlink(missing_ok=True)  # 删除即删文件：照片可能拍到人，不留副本

    def _file_of(self, photo: dict[str, Any]) -> tuple[Path, str]:
        ext = EXTENSIONS[str(photo["mime"])]
        return self.store.photo_dir / f"{photo['id']}.{ext}", str(photo["mime"])

    # ---------- 管理员 ----------

    @property
    def admin_enabled(self) -> bool:
        return bool(self.admin_token)

    def require_admin(self, token: str | None, viewer: Viewer) -> None:
        if not self.admin_token:
            raise MarkingError(
                503,
                "admin_disabled",
                "审核功能未开启：请在 .env 设置至少 12 位的 ADMIN_TOKEN 并重启后端",
            )
        limit, window = trust.ADMIN_FAIL_LIMIT
        key = f"i:{viewer.ip}"
        if self.limiter.blocked("admin_fail", key, limit, window):
            raise MarkingError(429, "rate_limited", "管理员口令输错次数太多，请 10 分钟后再试")
        if not token or not hmac.compare_digest(
            token.encode("utf-8"), self.admin_token.encode("utf-8")
        ):
            with contextlib.suppress(trust.RateLimited):
                self.limiter.hit("admin_fail", key, limit, window)
            raise MarkingError(401, "admin_denied", "管理员口令不对")

    def admin_queue(self, queue: str, viewer: Viewer) -> dict[str, Any]:
        if queue not in ADMIN_QUEUES:
            raise MarkingError(400, "invalid", "未知的审核队列")
        now = now_beijing()
        rows = self.store.by_status(
            ("pending", "verified", "rejected", "archived", "retracted"), limit=2000
        )

        def bucket(row: dict[str, Any]) -> set[str]:
            eff = trust.effective_status(row, now)
            names = {eff}
            if eff in trust.ACTIVE and trust.is_disputed(row):
                names.add("disputed")
            return names

        counts = {q: 0 for q in ADMIN_QUEUES}
        chosen = []
        for row in rows:
            names = bucket(row)
            for name in names:
                if name in counts:
                    counts[name] += 1
            if queue in names:
                chosen.append(row)
        items = self.publics(chosen, viewer, now)
        # 有争议、带照片、等得久的排前面
        items.sort(key=lambda m: (not m["disputed"], -m["photo_count"], m["created_at"]))
        return {"queue": queue, "items": items[:200], "counts": counts}

    def admin_detail(self, marking_id: int, viewer: Viewer) -> dict[str, Any]:
        row = self._row(marking_id)
        now = now_beijing()
        item = self.publics([row], viewer, now)[0]
        item["photos"] = [
            self._photo_public(p, viewer)
            for p in self.store.photos(marking_id, ("visible", "hidden"))
        ]
        events = self.store.events(marking_id)
        item["events"] = self._timeline(row, events, admin=True)
        item["versions"] = [
            {
                "version": v["version"],
                "title": models.title(v["spec"]),
                "spec": v["spec"],
                "at": v["created_at"],
            }
            for v in self.store.versions(marking_id)
        ]
        history = self.store.by_author(row["author_hash"], limit=500)
        item["author_history"] = {
            "total": len(history),
            "verified": sum(1 for r in history if r["status"] == "verified"),
            "rejected": sum(1 for r in history if r["status"] == "rejected"),
        }
        item["self_submitted"] = bool(viewer.device_hash) and (
            viewer.device_hash == row["author_hash"]
        )
        opened = time.monotonic()
        with self._lock:
            # 顺手清掉超时的记录，长期运行也不会越积越多
            for key in [k for k, t in self._tickets.items() if opened - t[0] > REVIEW_TICKET_TTL_S]:
                del self._tickets[key]
            self._tickets[(marking_id, viewer.device_hash)] = (opened, int(row["version"]))
        item["review_ticket"] = {"opened_at": iso(now), "min_seconds": REVIEW_MIN_SECONDS}
        return item

    def admin_review(self, marking_id: int, body: dict[str, Any], viewer: Viewer) -> dict[str, Any]:
        decision = body.get("decision")
        if decision not in ("verify", "reject", "archive", "reopen"):
            raise MarkingError(400, "invalid", "未知的审核决定")
        try:
            note = models.clean_text(body.get("note"), REVIEW_NOTE_MAX, "审核意见", required=True)
        except MarkingValidationError as exc:
            raise _invalid(exc) from exc
        if len(note) < REVIEW_NOTE_MIN:
            raise MarkingError(
                400, "invalid", f"审核意见至少写 {REVIEW_NOTE_MIN} 个字，说明你依据什么做的判断"
            )
        row = self._row(marking_id)
        version = body.get("version")
        if version != int(row["version"]):
            raise MarkingError(
                409,
                "version_conflict",
                "这条标注在你审核期间被修改过，请重新打开查看最新内容",
                current_version=int(row["version"]),
            )
        now = now_beijing()
        effective = trust.effective_status(row, now)
        payload: dict[str, Any] = {"note": note}
        expires: str | None = None

        if decision == "verify":
            self._check_verify(marking_id, row, body, viewer, effective)
            payload.update(
                basis=list(body["basis"]),
                checks={k: True for k in REVIEW_CHECKS},
                photos_reviewed=len(body.get("photos_reviewed") or []),
            )
            renew = body.get("expires_in_days")
            if effective == "expired" and renew is None:
                raise MarkingError(400, "invalid", "这条标注已过期，核实时请重新设定有效期")
            if renew is not None:
                try:
                    days = models.ttl_days(row["spec"], renew)
                except MarkingValidationError as exc:
                    raise _invalid(exc) from exc
                expires = iso(models.expires_at(now, days))
            from_statuses: tuple[str, ...] = ("pending",)
            to_status = "verified"
        elif decision == "reject":
            reason = body.get("reason")
            if reason not in REJECT_REASONS:
                raise MarkingError(400, "invalid", "请选择驳回原因")
            payload["reason"] = reason
            from_statuses, to_status = trust.ACTIVE, "rejected"
        elif decision == "archive":
            from_statuses, to_status = trust.ACTIVE, "archived"
        else:
            from_statuses, to_status = ("rejected", "archived"), "pending"

        try:
            self.store.transition(
                marking_id,
                from_statuses=from_statuses,
                to_status=to_status,
                now=iso(now),
                actor="admin",
                actor_hash=viewer.device_hash,
                action=decision,
                payload=payload,
                expected_version=int(row["version"]),
                expires_at=expires,
                reviewed=True,
            )
        except StatusConflict as exc:
            raise MarkingError(
                409, "bad_status", f"这条标注{_status_label(exc.current)}，不能执行这个审核操作"
            ) from exc
        except VersionConflict as exc:
            raise self._conflict(exc.current) from exc
        with self._lock:
            for key in [k for k in self._tickets if k[0] == marking_id]:
                del self._tickets[key]
        return self.admin_detail(marking_id, viewer)

    def _check_verify(
        self,
        marking_id: int,
        row: dict[str, Any],
        body: dict[str, Any],
        viewer: Viewer,
        effective: str,
    ) -> None:
        if row["status"] != "pending":
            raise MarkingError(
                409, "bad_status", f"只有待核实的标注可以核实，这条{_status_label(effective)}"
            )
        # 核实必须带设备标识：不带就无法判断是不是自己提交的
        device = self._device(viewer)
        if device == row["author_hash"]:
            raise MarkingError(403, "own_marking", "不能核实自己这台设备提交的标注")
        with self._lock:
            ticket = self._tickets.get((marking_id, device))
        elapsed = time.monotonic() - ticket[0] if ticket else None
        if ticket is None or elapsed is None or elapsed > REVIEW_TICKET_TTL_S:
            raise MarkingError(
                409, "review_not_opened", "请先打开这条标注的详情，查看照片与记录后再核实"
            )
        if ticket[1] != int(row["version"]):
            raise MarkingError(
                409, "review_stale", "你打开详情之后这条标注被修改过，请重新打开查看"
            )
        if elapsed < REVIEW_MIN_SECONDS:
            raise MarkingError(
                409,
                "review_too_fast",
                f"请先看完照片与记录再核实（打开详情至少 {REVIEW_MIN_SECONDS:.0f} 秒）",
            )
        checks = body.get("checks") or {}
        missing = [k for k in REVIEW_CHECKS if checks.get(k) is not True]
        if missing:
            raise MarkingError(400, "invalid", "请逐项确认：位置准确、类型与描述相符、目前仍然有效")
        basis = body.get("basis") or []
        if not basis or any(b not in REVIEW_BASIS for b in basis) or len(set(basis)) != len(basis):
            raise MarkingError(400, "invalid", "请选择核实依据")
        if "photo" in basis:
            visible = {p["id"] for p in self.store.photos(marking_id)}
            if not visible:
                raise MarkingError(400, "invalid", "这条标注没有照片，依据不能选「现场照片」")
            unseen = visible - set(body.get("photos_reviewed") or [])
            if unseen:
                raise MarkingError(
                    400,
                    "photos_unseen",
                    f"依据选了「现场照片」，还有 {len(unseen)} 张照片没有查看",
                )
        if "multi_confirm" in basis and int(row["confirms"]) < 2:
            raise MarkingError(400, "invalid", "确认人数不到 2 人，依据不能选「多人确认」")

    def set_photo_visibility(
        self, photo_id: str, hidden: bool, note: str, viewer: Viewer
    ) -> dict[str, Any]:
        try:
            note = models.clean_text(note, REVIEW_NOTE_MAX, "原因", required=True)
        except MarkingValidationError as exc:
            raise _invalid(exc) from exc
        self._photo_row(photo_id)
        try:
            photo = self.store.set_photo_status(
                photo_id,
                from_statuses=("visible",) if hidden else ("hidden",),
                to_status="hidden" if hidden else "visible",
                now=iso(now_beijing()),
                actor="admin",
                actor_hash=viewer.device_hash,
                payload={"note": note},
            )
        except StatusConflict as exc:
            raise MarkingError(409, "bad_status", "照片状态已经变了，请刷新") from exc
        updated = self.store.photo(photo_id) or photo
        return self._photo_public(updated, viewer)

    # ---------- 分析 ----------

    def for_analysis(
        self, lat: float, lng: float, radius_m: float, device_hash: str | None
    ) -> list[dict[str, Any]]:
        """分析时用：中心附近所有有效（待核实 + 已核实、未过期）的标注。"""
        now = now_beijing()
        found = self._active_near(lat, lng, radius_m, now)
        return self.publics(
            [r for _, r in found], Viewer(device_hash, "analysis"), now, origin=(lat, lng)
        )

    def config(self) -> dict[str, Any]:
        return {
            **models.labels(),
            "status_labels": STATUS_LABELS,
            "review_basis": REVIEW_BASIS,
            "reject_reasons": REJECT_REASONS,
            "photo": {
                "required": MIN_PHOTOS > 0,
                "min_per_marking": MIN_PHOTOS,
                "max_bytes": MAX_BYTES,
                "max_per_marking": MAX_PHOTOS,
                "max_per_uploader": MAX_PHOTOS_PER_UPLOADER,
                "upload_ttl_s": UPLOAD_TTL_S,
                "formats": ["image/jpeg", "image/png", "image/webp"],
            },
            "review": {"min_seconds": REVIEW_MIN_SECONDS, "note_min": REVIEW_NOTE_MIN},
            "admin_enabled": self.admin_enabled,
        }
