"""分析历史记录（SQLite）。

实时计算结果落库，侧栏可随时回看：重复查看不重烧配额，也便于演示时
在多个中心点之间来回切换。库文件放在 .cache/ 下（已 gitignore），
丢失无妨——所有记录都可由实时计算再生。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from .config import PROJECT_ROOT

DB_PATH = PROJECT_ROOT / ".cache" / "analyses.db"
_LOCK = threading.Lock()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with _LOCK, _connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                lat REAL NOT NULL,
                lng REAL NOT NULL,
                minutes REAL NOT NULL,
                mode TEXT NOT NULL,
                area_km2 REAL,
                score REAL,
                payload_json TEXT NOT NULL,
                result_json TEXT NOT NULL
            )
            """
        )
        con.commit()


def save_analysis(payload: dict[str, Any], result: dict[str, Any]) -> int:
    """保存一次实时计算。payload 是请求参数，result 是完整 Feature。"""
    props = result.get("properties", {})
    report = props.get("report") or {}
    created = datetime.now(timezone.utc).isoformat()
    with _LOCK, _connect() as con:
        cur = con.execute(
            """
            INSERT INTO analyses(
                created_at, lat, lng, minutes, mode, area_km2, score,
                payload_json, result_json
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                created,
                payload.get("lat"),
                payload.get("lng"),
                payload.get("minutes"),
                payload.get("mode"),
                props.get("area_km2"),
                report.get("total"),
                json.dumps(payload, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
            ),
        )
        con.commit()
        return int(cur.lastrowid)


def list_analyses(limit: int = 20) -> list[dict[str, Any]]:
    """历史列表（轻量元数据，不含完整结果）。"""
    init_db()
    with _connect() as con:
        rows = con.execute(
            """
            SELECT id, created_at, lat, lng, minutes, mode, area_km2, score
            FROM analyses ORDER BY id DESC LIMIT ?
            """,
            (min(100, max(1, limit)),),
        ).fetchall()
    return [dict(r) for r in rows]


def get_analysis(analysis_id: int) -> dict[str, Any] | None:
    init_db()
    with _connect() as con:
        row = con.execute(
            "SELECT * FROM analyses WHERE id=?", (analysis_id,)
        ).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["payload"] = json.loads(data.pop("payload_json"))
    data["result"] = json.loads(data.pop("result_json"))
    return data
