"""标注的持久化（SQLite，WAL）。

数据放在 data/user/，不放 .cache/：缓存可以随手清掉再生，用户标注丢了就找不回来。

六张表：
- markings：每条标注的当前状态，spec 是权威内容，其余列是为查询推导出的冗余；
- marking_versions：每次修改存一版完整 spec，撤销修改就是回到上一版；
- marking_events：只追加的事件日志（新建、修改、撤回、恢复、审核、照片……），状态可追溯；
- marking_votes：每台设备对每条标注一票（确认 / 有异议），可改票；
- marking_photos：照片元数据，文件本身在 photos/ 下，按随机 ID 命名；
- photo_uploads：新建标注前先传上来、还没挂到标注上的照片。新建时在同一个事务里
  挂上去，所以不会出现「标注建好了、照片没传上」的半截状态（现场照片是必填的）。

写操作一律走 BEGIN IMMEDIATE 事务，修改带版本号做乐观并发：两个人同时改同一条，
后提交的那个会收到冲突而不是悄悄覆盖。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..config import get_settings

SCHEMA_VERSION = 2

# 测试可以把它指到临时目录；None 表示用配置里的 MARKINGS_DIR
DATA_DIR: Path | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS markings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    type         TEXT NOT NULL,
    kind         TEXT,
    category     TEXT,
    spec         TEXT NOT NULL,
    geometry     TEXT NOT NULL,
    lat          REAL NOT NULL,
    lng          REAL NOT NULL,
    min_lat      REAL NOT NULL,
    max_lat      REAL NOT NULL,
    min_lng      REAL NOT NULL,
    max_lng      REAL NOT NULL,
    note         TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL,
    status       TEXT NOT NULL,
    version      INTEGER NOT NULL DEFAULT 1,
    author_hash  TEXT NOT NULL,
    edit_hash    TEXT NOT NULL,
    confirms     INTEGER NOT NULL DEFAULT 0,
    disputes     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    reviewed_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_markings_bbox ON markings (min_lat, max_lat, min_lng, max_lng);
CREATE INDEX IF NOT EXISTS idx_markings_author ON markings (author_hash);
CREATE INDEX IF NOT EXISTS idx_markings_status ON markings (status);

CREATE TABLE IF NOT EXISTS marking_versions (
    marking_id INTEGER NOT NULL REFERENCES markings(id),
    version    INTEGER NOT NULL,
    spec       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (marking_id, version)
);

CREATE TABLE IF NOT EXISTS marking_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    marking_id INTEGER NOT NULL REFERENCES markings(id),
    action     TEXT NOT NULL,
    actor      TEXT NOT NULL,
    actor_hash TEXT,
    payload    TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_marking ON marking_events (marking_id, id);

CREATE TABLE IF NOT EXISTS marking_votes (
    marking_id INTEGER NOT NULL REFERENCES markings(id),
    voter_hash TEXT NOT NULL,
    vote       INTEGER NOT NULL CHECK (vote IN (-1, 1)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (marking_id, voter_hash)
);

CREATE TABLE IF NOT EXISTS marking_photos (
    id            TEXT PRIMARY KEY,
    marking_id    INTEGER NOT NULL REFERENCES markings(id),
    uploader_hash TEXT NOT NULL,
    role          TEXT NOT NULL,
    mime          TEXT NOT NULL,
    bytes         INTEGER NOT NULL,
    width         INTEGER NOT NULL,
    height        INTEGER NOT NULL,
    sha256        TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'visible',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_photos_marking ON marking_photos (marking_id);

-- 第 2 版：先传后建。文件已经在 photos/ 下（文件名就是最终的照片 ID），挂上标注时只搬这一行
CREATE TABLE IF NOT EXISTS photo_uploads (
    id            TEXT PRIMARY KEY,
    uploader_hash TEXT NOT NULL,
    mime          TEXT NOT NULL,
    bytes         INTEGER NOT NULL,
    width         INTEGER NOT NULL,
    height        INTEGER NOT NULL,
    sha256        TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_uploads_uploader ON photo_uploads (uploader_hash);
"""


class VersionConflict(Exception):
    """修改时版本号对不上：别人（或自己的另一个页面）已经先改过了。"""

    def __init__(self, current: int) -> None:
        super().__init__(f"current version {current}")
        self.current = current


class StatusConflict(Exception):
    """状态已经变了（比如刚被撤回），这次状态迁移不再成立。"""

    def __init__(self, current: str) -> None:
        super().__init__(f"current status {current}")
        self.current = current


class PhotoLimit(Exception):
    """照片超出数量上限（total / uploader / pending）、重复（duplicate），
    或删了就一张可见照片都不剩（last）。"""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class UploadMissing(Exception):
    """新建时引用的预传照片不存在、不是这台设备传的，或已经过期。"""

    def __init__(self, upload_ids: list[str]) -> None:
        super().__init__(", ".join(upload_ids))
        self.upload_ids = upload_ids


def data_root() -> Path:
    return DATA_DIR if DATA_DIR is not None else get_settings().markings_dir


def _decode(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    for key in ("spec", "geometry", "payload"):
        if key in data and isinstance(data[key], str):
            data[key] = json.loads(data[key])
    return data


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class MarkingStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.db_path = root / "markings.db"
        self.photo_dir = root / "photos"
        self._lock = threading.Lock()
        self._ready = False

    # ---------- 连接与事务 ----------

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = 10000")
        return con

    def init(self) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            self.root.mkdir(parents=True, exist_ok=True)
            self.photo_dir.mkdir(parents=True, exist_ok=True)
            con = self._connect()
            try:
                con.execute("PRAGMA journal_mode = WAL")
                con.executescript(SCHEMA)
                current = con.execute("PRAGMA user_version").fetchone()[0]
                if current < SCHEMA_VERSION:
                    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            finally:
                con.close()
            self._ready = True

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        self.init()
        con = self._connect()
        try:
            yield con
        finally:
            con.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self.init()
        with self._lock:
            con = self._connect()
            try:
                con.execute("BEGIN IMMEDIATE")
                try:
                    yield con
                except BaseException:
                    con.execute("ROLLBACK")
                    raise
                con.execute("COMMIT")
            finally:
                con.close()

    @staticmethod
    def _event(
        con: sqlite3.Connection,
        marking_id: int,
        action: str,
        actor: str,
        actor_hash: str | None,
        payload: dict[str, Any] | None,
        now: str,
    ) -> None:
        con.execute(
            "INSERT INTO marking_events(marking_id, action, actor, actor_hash, payload, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (marking_id, action, actor, actor_hash, _dump(payload or {}), now),
        )

    # ---------- 标注 ----------

    def insert(
        self,
        spec: dict[str, Any],
        columns: dict[str, Any],
        *,
        source: str,
        author_hash: str,
        edit_hash: str,
        now: str,
        expires_at: str,
        uploads: list[str] | None = None,
        uploads_since: str | None = None,
    ) -> int:
        """新建一条标注，并把预传的照片 uploads 挂上去（作者照片）。

        照片只认这台设备、在 uploads_since 之后传的；有一张对不上就抛 UploadMissing，
        整个事务回滚——标注不会建出来，预传的照片也原样留着，可以改了再提交。
        """
        with self._tx() as con:
            staged: list[sqlite3.Row] = []
            if uploads:
                marks = ",".join("?" for _ in uploads)
                rows = {
                    r["id"]: r
                    for r in con.execute(
                        f"SELECT * FROM photo_uploads WHERE id IN ({marks}) AND uploader_hash=?",
                        (*uploads, author_hash),
                    ).fetchall()
                }
                missing = [
                    u
                    for u in uploads
                    if u not in rows or (uploads_since and rows[u]["created_at"] < uploads_since)
                ]
                if missing:
                    raise UploadMissing(missing)
                staged = [rows[u] for u in uploads]
                if len({r["sha256"] for r in staged}) != len(staged):
                    raise PhotoLimit("duplicate")
            cur = con.execute(
                """
                INSERT INTO markings(
                    type, kind, category, spec, geometry, lat, lng,
                    min_lat, max_lat, min_lng, max_lng, note, source, status, version,
                    author_hash, edit_hash, created_at, updated_at, expires_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',1,?,?,?,?,?)
                """,
                (
                    columns["type"],
                    columns["kind"],
                    columns["category"],
                    _dump(spec),
                    _dump(columns["geometry"]),
                    columns["lat"],
                    columns["lng"],
                    columns["min_lat"],
                    columns["max_lat"],
                    columns["min_lng"],
                    columns["max_lng"],
                    columns["note"],
                    source,
                    author_hash,
                    edit_hash,
                    now,
                    now,
                    expires_at,
                ),
            )
            mid = int(cur.lastrowid)
            con.execute(
                "INSERT INTO marking_versions(marking_id, version, spec, created_at)"
                " VALUES(?,?,?,?)",
                (mid, 1, _dump(spec), now),
            )
            self._event(
                con,
                mid,
                "create",
                "author",
                author_hash,
                {"source": source, "photos": len(staged)},
                now,
            )
            for r in staged:
                con.execute(
                    """
                    INSERT INTO marking_photos(
                        id, marking_id, uploader_hash, role, mime, bytes, width, height,
                        sha256, status, created_at
                    ) VALUES(?,?,?,'author',?,?,?,?,?,'visible',?)
                    """,
                    (
                        r["id"],
                        mid,
                        author_hash,
                        r["mime"],
                        r["bytes"],
                        r["width"],
                        r["height"],
                        r["sha256"],
                        now,
                    ),
                )
                con.execute("DELETE FROM photo_uploads WHERE id=?", (r["id"],))
            return mid

    def get(self, marking_id: int) -> dict[str, Any] | None:
        with self._read() as con:
            row = con.execute("SELECT * FROM markings WHERE id=?", (marking_id,)).fetchone()
        return _decode(row)

    def in_bbox(
        self,
        min_lat: float,
        max_lat: float,
        min_lng: float,
        max_lng: float,
        statuses: tuple[str, ...],
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """外接框与查询框相交、且状态在 statuses 里的标注（粗筛，精确距离由调用方算）。"""
        marks = ",".join("?" for _ in statuses)
        with self._read() as con:
            rows = con.execute(
                f"""
                SELECT * FROM markings
                WHERE max_lat >= ? AND min_lat <= ? AND max_lng >= ? AND min_lng <= ?
                  AND status IN ({marks})
                ORDER BY id DESC LIMIT ?
                """,
                (min_lat, max_lat, min_lng, max_lng, *statuses, limit),
            ).fetchall()
        return [d for d in (_decode(r) for r in rows) if d is not None]

    def by_author(self, author_hash: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._read() as con:
            rows = con.execute(
                "SELECT * FROM markings WHERE author_hash=? ORDER BY id DESC LIMIT ?",
                (author_hash, limit),
            ).fetchall()
        return [d for d in (_decode(r) for r in rows) if d is not None]

    def by_status(self, statuses: tuple[str, ...], limit: int = 200) -> list[dict[str, Any]]:
        marks = ",".join("?" for _ in statuses)
        with self._read() as con:
            rows = con.execute(
                f"SELECT * FROM markings WHERE status IN ({marks}) ORDER BY id ASC LIMIT ?",
                (*statuses, limit),
            ).fetchall()
        return [d for d in (_decode(r) for r in rows) if d is not None]

    def update_spec(
        self,
        marking_id: int,
        *,
        expected_version: int,
        spec: dict[str, Any],
        columns: dict[str, Any],
        status: str,
        now: str,
        actor: str,
        actor_hash: str | None,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> int:
        """写入新版本内容。版本号对不上抛 VersionConflict。返回新版本号。"""
        with self._tx() as con:
            new_version = expected_version + 1
            cur = con.execute(
                """
                UPDATE markings SET kind=?, category=?, spec=?, geometry=?, lat=?, lng=?,
                    min_lat=?, max_lat=?, min_lng=?, max_lng=?, note=?, status=?,
                    version=?, updated_at=?
                WHERE id=? AND version=?
                """,
                (
                    columns["kind"],
                    columns["category"],
                    _dump(spec),
                    _dump(columns["geometry"]),
                    columns["lat"],
                    columns["lng"],
                    columns["min_lat"],
                    columns["max_lat"],
                    columns["min_lng"],
                    columns["max_lng"],
                    columns["note"],
                    status,
                    new_version,
                    now,
                    marking_id,
                    expected_version,
                ),
            )
            if cur.rowcount != 1:
                row = con.execute(
                    "SELECT version FROM markings WHERE id=?", (marking_id,)
                ).fetchone()
                raise VersionConflict(int(row["version"]) if row else -1)
            con.execute(
                "INSERT INTO marking_versions(marking_id, version, spec, created_at)"
                " VALUES(?,?,?,?)",
                (marking_id, new_version, _dump(spec), now),
            )
            self._event(
                con,
                marking_id,
                action,
                actor,
                actor_hash,
                {**(payload or {}), "version": new_version},
                now,
            )
            return new_version

    def transition(
        self,
        marking_id: int,
        *,
        from_statuses: tuple[str, ...],
        to_status: str,
        now: str,
        actor: str,
        actor_hash: str | None,
        action: str,
        payload: dict[str, Any] | None = None,
        expected_version: int | None = None,
        expires_at: str | None = None,
        reviewed: bool = False,
    ) -> None:
        """状态迁移。当前状态不在 from_statuses 里抛 StatusConflict，版本不符抛 VersionConflict。"""
        with self._tx() as con:
            row = con.execute(
                "SELECT status, version FROM markings WHERE id=?", (marking_id,)
            ).fetchone()
            if row is None:
                raise StatusConflict("missing")
            if row["status"] not in from_statuses:
                raise StatusConflict(str(row["status"]))
            if expected_version is not None and int(row["version"]) != expected_version:
                raise VersionConflict(int(row["version"]))
            sets = ["status=?", "updated_at=?"]
            params: list[Any] = [to_status, now]
            if expires_at is not None:
                sets.append("expires_at=?")
                params.append(expires_at)
            if reviewed:
                sets.append("reviewed_at=?")
                params.append(now)
            con.execute(f"UPDATE markings SET {', '.join(sets)} WHERE id=?", (*params, marking_id))
            self._event(
                con,
                marking_id,
                action,
                actor,
                actor_hash,
                {**(payload or {}), "from": row["status"], "to": to_status},
                now,
            )

    def set_expiry(
        self,
        marking_id: int,
        *,
        expected_version: int,
        expires_at: str,
        now: str,
        actor: str,
        actor_hash: str | None,
    ) -> None:
        with self._tx() as con:
            cur = con.execute(
                "UPDATE markings SET expires_at=?, updated_at=? WHERE id=? AND version=?",
                (expires_at, now, marking_id, expected_version),
            )
            if cur.rowcount != 1:
                row = con.execute(
                    "SELECT version FROM markings WHERE id=?", (marking_id,)
                ).fetchone()
                raise VersionConflict(int(row["version"]) if row else -1)
            self._event(
                con, marking_id, "renew", actor, actor_hash, {"expires_at": expires_at}, now
            )

    def version_spec(self, marking_id: int, version: int) -> dict[str, Any] | None:
        with self._read() as con:
            row = con.execute(
                "SELECT spec FROM marking_versions WHERE marking_id=? AND version=?",
                (marking_id, version),
            ).fetchone()
        return json.loads(row["spec"]) if row else None

    def versions(self, marking_id: int) -> list[dict[str, Any]]:
        with self._read() as con:
            rows = con.execute(
                "SELECT version, spec, created_at FROM marking_versions"
                " WHERE marking_id=? ORDER BY version",
                (marking_id,),
            ).fetchall()
        return [d for d in (_decode(r) for r in rows) if d is not None]

    def events(self, marking_id: int) -> list[dict[str, Any]]:
        with self._read() as con:
            rows = con.execute(
                "SELECT id, action, actor, actor_hash, payload, created_at FROM marking_events"
                " WHERE marking_id=? ORDER BY id",
                (marking_id,),
            ).fetchall()
        return [d for d in (_decode(r) for r in rows) if d is not None]

    def last_events(
        self, marking_ids: list[int], actions: tuple[str, ...]
    ) -> dict[int, dict[str, Any]]:
        """每条标注最近一次指定动作的事件。"""
        if not marking_ids:
            return {}
        ids = ",".join("?" for _ in marking_ids)
        acts = ",".join("?" for _ in actions)
        with self._read() as con:
            rows = con.execute(
                f"""
                SELECT e.* FROM marking_events e
                JOIN (
                    SELECT marking_id, MAX(id) AS mid FROM marking_events
                    WHERE marking_id IN ({ids}) AND action IN ({acts})
                    GROUP BY marking_id
                ) last ON last.mid = e.id
                """,
                (*marking_ids, *actions),
            ).fetchall()
        out: dict[int, dict[str, Any]] = {}
        for r in rows:
            d = _decode(r)
            if d is not None:
                out[int(d["marking_id"])] = d
        return out

    # ---------- 投票 ----------

    def set_vote(self, marking_id: int, voter_hash: str, vote: int, now: str) -> tuple[int, int]:
        """设置一台设备的票（1 确认、-1 有异议、0 撤票），返回新的 (确认数, 异议数)。"""
        with self._tx() as con:
            prev = con.execute(
                "SELECT vote FROM marking_votes WHERE marking_id=? AND voter_hash=?",
                (marking_id, voter_hash),
            ).fetchone()
            if vote == 0:
                con.execute(
                    "DELETE FROM marking_votes WHERE marking_id=? AND voter_hash=?",
                    (marking_id, voter_hash),
                )
            else:
                con.execute(
                    """
                    INSERT INTO marking_votes(marking_id, voter_hash, vote, created_at)
                    VALUES(?,?,?,?)
                    ON CONFLICT(marking_id, voter_hash)
                    DO UPDATE SET vote=excluded.vote, created_at=excluded.created_at
                    """,
                    (marking_id, voter_hash, vote, now),
                )
            counts = con.execute(
                """
                SELECT COALESCE(SUM(vote = 1), 0) AS c, COALESCE(SUM(vote = -1), 0) AS d
                FROM marking_votes WHERE marking_id=?
                """,
                (marking_id,),
            ).fetchone()
            confirms, disputes = int(counts["c"]), int(counts["d"])
            con.execute(
                "UPDATE markings SET confirms=?, disputes=? WHERE id=?",
                (confirms, disputes, marking_id),
            )
            if (prev["vote"] if prev else 0) != vote:
                action = {1: "confirm", -1: "dispute", 0: "unvote"}[vote]
                self._event(con, marking_id, action, "voter", voter_hash, {}, now)
            return confirms, disputes

    def votes_of(self, marking_ids: list[int], voter_hash: str) -> dict[int, int]:
        if not marking_ids:
            return {}
        ids = ",".join("?" for _ in marking_ids)
        with self._read() as con:
            rows = con.execute(
                f"SELECT marking_id, vote FROM marking_votes"
                f" WHERE voter_hash=? AND marking_id IN ({ids})",
                (voter_hash, *marking_ids),
            ).fetchall()
        return {int(r["marking_id"]): int(r["vote"]) for r in rows}

    # ---------- 照片 ----------

    def add_photo(
        self,
        photo: dict[str, Any],
        *,
        actor: str,
        now: str,
        max_total: int,
        max_per_uploader: int,
    ) -> None:
        """登记一张照片。数量上限与重复检查放在同一个事务里，并发上传也不会超限。"""
        with self._tx() as con:
            live = con.execute(
                "SELECT uploader_hash, sha256 FROM marking_photos"
                " WHERE marking_id=? AND status != 'deleted'",
                (photo["marking_id"],),
            ).fetchall()
            if any(r["sha256"] == photo["sha256"] for r in live):
                raise PhotoLimit("duplicate")
            if len(live) >= max_total:
                raise PhotoLimit("total")
            if sum(1 for r in live if r["uploader_hash"] == photo["uploader_hash"]) >= (
                max_per_uploader
            ):
                raise PhotoLimit("uploader")
            con.execute(
                """
                INSERT INTO marking_photos(
                    id, marking_id, uploader_hash, role, mime, bytes, width, height,
                    sha256, status, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,'visible',?)
                """,
                (
                    photo["id"],
                    photo["marking_id"],
                    photo["uploader_hash"],
                    photo["role"],
                    photo["mime"],
                    photo["bytes"],
                    photo["width"],
                    photo["height"],
                    photo["sha256"],
                    now,
                ),
            )
            self._event(
                con,
                photo["marking_id"],
                "photo_add",
                actor,
                photo["uploader_hash"],
                {"photo": photo["id"], "role": photo["role"]},
                now,
            )

    def photos(
        self, marking_id: int, statuses: tuple[str, ...] = ("visible",)
    ) -> list[dict[str, Any]]:
        marks = ",".join("?" for _ in statuses)
        with self._read() as con:
            rows = con.execute(
                f"SELECT * FROM marking_photos WHERE marking_id=? AND status IN ({marks})"
                " ORDER BY created_at, id",
                (marking_id, *statuses),
            ).fetchall()
        return [dict(r) for r in rows]

    def photo(self, photo_id: str) -> dict[str, Any] | None:
        with self._read() as con:
            row = con.execute("SELECT * FROM marking_photos WHERE id=?", (photo_id,)).fetchone()
        return dict(row) if row else None

    def photo_counts(self, marking_ids: list[int]) -> dict[int, int]:
        if not marking_ids:
            return {}
        ids = ",".join("?" for _ in marking_ids)
        with self._read() as con:
            rows = con.execute(
                f"SELECT marking_id, COUNT(*) AS n FROM marking_photos"
                f" WHERE status='visible' AND marking_id IN ({ids}) GROUP BY marking_id",
                tuple(marking_ids),
            ).fetchall()
        return {int(r["marking_id"]): int(r["n"]) for r in rows}

    def set_photo_status(
        self,
        photo_id: str,
        *,
        from_statuses: tuple[str, ...],
        to_status: str,
        now: str,
        actor: str,
        actor_hash: str | None,
        payload: dict[str, Any] | None = None,
        keep_one_visible: bool = False,
    ) -> dict[str, Any]:
        """改一张照片的状态。keep_one_visible：改完这条标注必须还剩至少一张可见照片，
        否则抛 PhotoLimit("last")。计数与修改在同一个事务里，两个页面同时删也删不光。"""
        with self._tx() as con:
            row = con.execute("SELECT * FROM marking_photos WHERE id=?", (photo_id,)).fetchone()
            if row is None:
                raise StatusConflict("missing")
            if row["status"] not in from_statuses:
                raise StatusConflict(str(row["status"]))
            if keep_one_visible and row["status"] == "visible" and to_status != "visible":
                others = con.execute(
                    "SELECT COUNT(*) FROM marking_photos"
                    " WHERE marking_id=? AND status='visible' AND id != ?",
                    (row["marking_id"], photo_id),
                ).fetchone()[0]
                if others == 0:
                    raise PhotoLimit("last")
            con.execute("UPDATE marking_photos SET status=? WHERE id=?", (to_status, photo_id))
            action = {"deleted": "photo_delete", "hidden": "photo_hide", "visible": "photo_show"}[
                to_status
            ]
            self._event(
                con,
                int(row["marking_id"]),
                action,
                actor,
                actor_hash,
                {**(payload or {}), "photo": photo_id},
                now,
            )
            return dict(row)

    # ---------- 预传照片 ----------

    def stage_upload(self, upload: dict[str, Any], *, now: str, max_pending: int) -> None:
        """登记一张预传照片。每台设备同时挂着的预传照片有上限，免得有人只传不建、占满磁盘。"""
        with self._tx() as con:
            pending = con.execute(
                "SELECT COUNT(*) FROM photo_uploads WHERE uploader_hash=?",
                (upload["uploader_hash"],),
            ).fetchone()[0]
            if pending >= max_pending:
                raise PhotoLimit("pending")
            con.execute(
                """
                INSERT INTO photo_uploads(
                    id, uploader_hash, mime, bytes, width, height, sha256, created_at
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    upload["id"],
                    upload["uploader_hash"],
                    upload["mime"],
                    upload["bytes"],
                    upload["width"],
                    upload["height"],
                    upload["sha256"],
                    now,
                ),
            )

    def uploads(self, upload_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not upload_ids:
            return {}
        marks = ",".join("?" for _ in upload_ids)
        with self._read() as con:
            rows = con.execute(
                f"SELECT * FROM photo_uploads WHERE id IN ({marks})", tuple(upload_ids)
            ).fetchall()
        return {str(r["id"]): dict(r) for r in rows}

    def take_expired_uploads(self, before: str) -> list[dict[str, Any]]:
        """取出并删掉 before 之前传、一直没挂上标注的预传照片，返回它们（调用方删文件）。"""
        with self._tx() as con:
            rows = con.execute(
                "SELECT id, mime FROM photo_uploads WHERE created_at < ?", (before,)
            ).fetchall()
            con.execute("DELETE FROM photo_uploads WHERE created_at < ?", (before,))
        return [dict(r) for r in rows]
