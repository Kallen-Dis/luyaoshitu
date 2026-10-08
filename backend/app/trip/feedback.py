"""已收录门店的售菜观察；不创建共享设施、不改变推荐资格。"""

from __future__ import annotations

import hashlib
import sqlite3
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

from ..isochrone.geometry import haversine_m, meters_per_deg_lng
from ..markings.models import now_beijing

WINDOW_DAYS = 90
MATCH_M = 30
SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback_places (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, normalized_name TEXT NOT NULL,
    lat REAL NOT NULL, lng REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_name ON feedback_places(normalized_name);
CREATE TABLE IF NOT EXISTS fresh_observations (
    place_id TEXT NOT NULL REFERENCES feedback_places(id),
    device_hash TEXT NOT NULL, vote INTEGER NOT NULL CHECK(vote IN (-1, 1)),
    observed_at TEXT NOT NULL, PRIMARY KEY(place_id, device_hash)
);
"""


def normalized_name(name: str) -> str:
    # 保留分店后缀，不能把同址、同品牌的不同门店合并。
    return "".join(unicodedata.normalize("NFKC", name).split()).casefold()


class FreshFeedbackStore:
    def __init__(self, root: Path):
        self.path = root / "fresh-feedback.sqlite3"

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.executescript(SCHEMA)
        return con

    @staticmethod
    def _find(con, place):
        lat, lng = place["lat"], place["lng"]
        dlat, dlng = MATCH_M / 111_000, MATCH_M / meters_per_deg_lng(lat) * 1.01
        rows = con.execute(
            "SELECT * FROM feedback_places WHERE normalized_name=? "
            "AND lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?",
            (normalized_name(place["name"]), lat - dlat, lat + dlat, lng - dlng, lng + dlng),
        ).fetchall()
        candidates = [(haversine_m(lat, lng, row["lat"], row["lng"]), row) for row in rows]
        return min(
            (pair for pair in candidates if pair[0] <= MATCH_M),
            key=lambda p: p[0],
            default=(None, None),
        )[1]

    @staticmethod
    def _summary(con, row, device_hash, now):
        empty = {
            "confirms": 0,
            "not_seen": 0,
            "my_feedback": 0,
            "latest_at": None,
            "window_days": WINDOW_DAYS,
        }
        if row is None:
            return empty
        since = (now - timedelta(days=WINDOW_DAYS)).isoformat()
        counts = con.execute(
            "SELECT SUM(vote=1) AS c, SUM(vote=-1) AS n, MAX(observed_at) AS latest "
            "FROM fresh_observations WHERE place_id=? AND observed_at>=?",
            (row["id"], since),
        ).fetchone()
        own = (
            con.execute(
                "SELECT vote FROM fresh_observations "
                "WHERE place_id=? AND device_hash=? AND observed_at>=?",
                (row["id"], device_hash, since),
            ).fetchone()
            if device_hash
            else None
        )
        return {
            **empty,
            "confirms": counts["c"] or 0,
            "not_seen": counts["n"] or 0,
            "latest_at": counts["latest"],
            "my_feedback": own["vote"] if own else 0,
        }

    def summaries(self, places: list[dict], device_hash: str | None, now: datetime | None = None):
        con = self._connect()
        try:
            con.execute("BEGIN")  # 批量计数和本设备观察来自同一个读取快照。
            return [
                self._summary(con, self._find(con, place), device_hash, now or now_beijing())
                for place in places
            ]
        finally:
            con.close()

    def vote(self, place: dict, device_hash: str, vote: int, now: datetime | None = None):
        now = now or now_beijing()
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = self._find(con, place)
            if row is None and vote:
                key = hashlib.sha256(
                    f"{normalized_name(place['name'])}|{place['lat']:.6f}|{place['lng']:.6f}".encode()
                ).hexdigest()[:24]
                con.execute(
                    "INSERT INTO feedback_places VALUES(?,?,?,?,?)",
                    (
                        key,
                        place["name"],
                        normalized_name(place["name"]),
                        place["lat"],
                        place["lng"],
                    ),
                )
                row = con.execute("SELECT * FROM feedback_places WHERE id=?", (key,)).fetchone()
            if row is not None:
                if vote == 0:
                    con.execute(
                        "DELETE FROM fresh_observations WHERE place_id=? AND device_hash=?",
                        (row["id"], device_hash),
                    )
                else:
                    con.execute(
                        "INSERT INTO fresh_observations VALUES(?,?,?,?) "
                        "ON CONFLICT(place_id,device_hash) DO UPDATE SET "
                        "vote=excluded.vote, observed_at=excluded.observed_at",
                        (row["id"], device_hash, vote, now.isoformat()),
                    )
            result = self._summary(con, row, device_hash, now)
            con.commit()
            return result
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
