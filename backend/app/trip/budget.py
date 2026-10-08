"""原子、跨 worker 的出行预算；数据库只存时间和数量，不含坐标。"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import Settings

BEIJING = timezone(timedelta(hours=8))


class BudgetExhausted(RuntimeError):
    """请求尚未发送；已测结果可以保留。"""


@dataclass
class Usage:
    matrix_pairs: int = 0
    matrix_requests: int = 0
    route_requests: int = 0
    cache_hits: int = 0
    poi_requests: int = 0


class TripBudget:
    def __init__(self, path: Path, settings: Settings) -> None:
        self.path = path
        self.settings = settings

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.execute(
            "CREATE TABLE IF NOT EXISTS trip_usage "
            "(at REAL NOT NULL, day TEXT NOT NULL, pairs INTEGER NOT NULL, routes INTEGER NOT NULL)"
        )
        db.execute("CREATE TABLE IF NOT EXISTS trip_calls (at REAL NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS trip_pois (at REAL NOT NULL, day TEXT NOT NULL)")
        return db

    def take_request(self) -> bool:
        """两个公开规划接口共用小时请求上限，缓存命中也计数。"""
        now = time.time()
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM trip_calls WHERE at<=?", (now - 3600,))
            count = db.execute("SELECT COUNT(*) FROM trip_calls").fetchone()[0]
            allowed = count < self.settings.trip_hour_requests
            if allowed:
                db.execute("INSERT INTO trip_calls VALUES (?)", (now,))
            db.execute("COMMIT")
            return allowed
        finally:
            if db.in_transaction:
                db.execute("ROLLBACK")
            db.close()

    def remaining(self, usage: Usage) -> dict[str, int]:
        now = time.time()
        day = datetime.fromtimestamp(now, BEIJING).date().isoformat()
        db = self._connect()
        try:
            row = db.execute(
                "SELECT COALESCE(SUM(CASE WHEN day=? THEN pairs ELSE 0 END),0), "
                "COALESCE(SUM(CASE WHEN at>? THEN pairs ELSE 0 END),0), "
                "COALESCE(SUM(CASE WHEN at>? THEN routes ELSE 0 END),0) FROM trip_usage",
                (day, now - 3600, now - 3600),
            ).fetchone()
        finally:
            db.close()
        return self._remaining(usage, row)

    def _remaining(self, usage: Usage, row: tuple) -> dict[str, int]:
        s = self.settings
        return {
            "day_pairs": max(0, s.trip_day_pairs - row[0]),
            "hour_pairs": max(0, s.trip_hour_pairs - row[1]),
            "request_pairs": max(0, s.trip_request_pairs - usage.matrix_pairs),
            "hour_routes": max(0, s.trip_hour_routes - row[2]),
            "request_routes": max(0, s.trip_request_routes - usage.route_requests),
        }

    def take(self, usage: Usage, pairs: int = 0, routes: int = 0) -> None:
        now = time.time()
        day = datetime.fromtimestamp(now, BEIJING).date().isoformat()
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            # 只保留当前日和最近一小时，跨零点的小时窗口不会被误清空。
            db.execute("DELETE FROM trip_usage WHERE day!=? AND at<=?", (day, now - 3600))
            row = db.execute(
                "SELECT COALESCE(SUM(CASE WHEN day=? THEN pairs ELSE 0 END),0), "
                "COALESCE(SUM(CASE WHEN at>? THEN pairs ELSE 0 END),0), "
                "COALESCE(SUM(CASE WHEN at>? THEN routes ELSE 0 END),0) FROM trip_usage",
                (day, now - 3600, now - 3600),
            ).fetchone()
            remaining = self._remaining(usage, row)
            for name in ("day_pairs", "hour_pairs", "request_pairs"):
                if pairs > remaining[name]:
                    raise BudgetExhausted(
                        {
                            "day_pairs": "出行日点对预算已用完，北京时间次日 0 点恢复。",
                            "hour_pairs": "出行小时点对预算不足，稍后可重试。",
                            "request_pairs": "本次出行点对预算不足，未完成排序验证。",
                        }[name]
                    )
            if routes > min(remaining["hour_routes"], remaining["request_routes"]):
                raise BudgetExhausted("出行路线请求预算不足，距离保留，折线暂未取得。")
            db.execute("INSERT INTO trip_usage VALUES (?,?,?,?)", (now, day, pairs, routes))
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()
        usage.matrix_pairs += pairs
        usage.matrix_requests += int(pairs > 0)
        usage.route_requests += routes

    def observe(self, usage: Usage, endpoint: str, params: dict) -> None:
        if endpoint == "/routematrix/v2/walking":
            pairs = len(params["origins"].split("|")) * len(params["destinations"].split("|"))
            self.take(usage, pairs=pairs)
        elif endpoint == "/directionlite/v1/walking":
            self.take(usage, routes=1)
        elif endpoint == "/place/v2/search":
            self.take_poi(usage)

    def take_poi(self, usage: Usage) -> None:
        """当前位置补检索单独限额；每页及重试均计数，缓存命中不计。"""
        now = time.time()
        day = datetime.fromtimestamp(now, BEIJING).date().isoformat()
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM trip_pois WHERE day!=? AND at<=?", (day, now - 3600))
            daily, hourly = db.execute(
                "SELECT COALESCE(SUM(day=?),0), COALESCE(SUM(at>?),0) FROM trip_pois",
                (day, now - 3600),
            ).fetchone()
            if daily >= self.settings.trip_day_pois:
                raise BudgetExhausted("出行设施补检索日预算已用完，北京时间次日恢复。")
            if hourly >= self.settings.trip_hour_pois:
                raise BudgetExhausted("出行设施补检索小时预算不足，请稍后重试。")
            if usage.poi_requests >= self.settings.trip_request_pois:
                raise BudgetExhausted("本次设施补检索预算不足，检索未完成。")
            db.execute("INSERT INTO trip_pois VALUES (?,?)", (now, day))
            db.execute("COMMIT")
            usage.poi_requests += 1
        finally:
            if db.in_transaction:
                db.execute("ROLLBACK")
            db.close()
