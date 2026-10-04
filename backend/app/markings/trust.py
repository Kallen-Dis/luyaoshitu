"""信任、身份与限流。

**身份。** 现阶段对任何人开放，没有账号：浏览器生成一个设备 ID（存 localStorage，
请求头 X-Device-Id），服务端只存加盐哈希。每条标注新建时另发一个一次性编辑令牌，
服务端只存它的哈希；修改、撤回、恢复都要出示它。这不是真正的认证，公开部署前要换成登录。

**信任。** 状态由管理员审核决定，投票不会让标注自动变成「已核实」：没有账号时，
换一个设备 ID 就能多投一票，靠票数自动生效等于谁都能让自己的标注对所有人生效。
投票只影响置信度、排序与「有争议」提示，并把有争议的标注顶到审核队列前面。

**有效期** 在读取时判断，不跑定时任务：过期的标注不再参与计算，作者可以续期。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any

from .models import parse_time

DEVICE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

# 置信度的先验 (a, b)：c = (a + 确认) / (a + b + 确认 + 异议)
PRIORS: dict[str, tuple[float, float]] = {
    "user": (1.0, 1.0),
    "recheck": (2.0, 1.0),
    "poi": (1.5, 1.0),
    # 两条召回通道里有一条找到了、又有人到现场拍了照，比凭空补录可信一点
    "agent_plan": (1.5, 1.0),
}
VERIFIED_PRIOR = (9.0, 1.0)

# 异议比确认多出这么多票，就标「有争议」
DISPUTE_MARGIN = 2

ACTIVE = ("pending", "verified")


def effective_status(row: dict[str, Any], now: datetime) -> str:
    """对外展示与参与计算用的状态：待核实 / 已核实的标注过了有效期记为 expired。"""
    status = str(row["status"])
    if status in ACTIVE:
        expires = parse_time(row.get("expires_at"))
        if expires is not None and now >= expires:
            return "expired"
    return status


def is_disputed(row: dict[str, Any]) -> bool:
    return int(row.get("disputes") or 0) >= int(row.get("confirms") or 0) + DISPUTE_MARGIN


def confidence(row: dict[str, Any], now: datetime | None = None) -> float:
    """0~1 的置信度，用于排序与展示；不决定是否生效。"""
    a, b = VERIFIED_PRIOR if row["status"] == "verified" else PRIORS.get(row["source"], (1, 1))
    confirms = int(row.get("confirms") or 0)
    disputes = int(row.get("disputes") or 0)
    value = (a + confirms) / (a + b + confirms + disputes)
    # 施工与封路会结束：离到期越近越不可信，最后 20% 的有效期里线性降到七成
    if now is not None and row.get("type") == "closure":
        created = parse_time(row.get("created_at"))
        expires = parse_time(row.get("expires_at"))
        if created and expires and expires > created:
            left = (expires - now).total_seconds() / (expires - created).total_seconds()
            if left < 0.2:
                value *= 0.7 + 1.5 * max(0.0, left)
    return round(max(0.0, min(1.0, value)), 3)


def hash_value(value: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{value}".encode()).hexdigest()


def new_edit_token() -> tuple[str, str]:
    """返回 (令牌明文, 令牌哈希)。明文只在新建时返回一次。"""
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode()).hexdigest()


def token_matches(token: str | None, stored_hash: str) -> bool:
    if not token or len(token) > 128:
        return False
    digest = hashlib.sha256(token.encode()).hexdigest()
    return hmac.compare_digest(digest, stored_hash)


class RateLimited(Exception):
    def __init__(self, retry_after_s: int) -> None:
        super().__init__(f"retry after {retry_after_s}s")
        self.retry_after_s = retry_after_s


class RateLimiter:
    """滑动窗口限流，进程内存实现。多实例部署时要换成 Redis。"""

    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, action: str, key: str, limit: int, window_s: float) -> None:
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault((action, key), deque())
            while q and now - q[0] >= window_s:
                q.popleft()
            if len(q) >= limit:
                raise RateLimited(max(1, int(window_s - (now - q[0])) + 1))
            q.append(now)

    def blocked(self, action: str, key: str, limit: int, window_s: float) -> bool:
        """只查不记：窗口内是否已达上限。"""
        now = time.monotonic()
        with self._lock:
            q = self._hits.get((action, key))
            if not q:
                return False
            while q and now - q[0] >= window_s:
                q.popleft()
            return len(q) >= limit

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


# 每小时上限：(动作, 每设备, 每 IP)
LIMITS: dict[str, tuple[int, int]] = {
    "create": (20, 40),
    "edit": (60, 120),
    "vote": (100, 200),
    "photo": (30, 60),
}
ADMIN_FAIL_LIMIT = (10, 600.0)  # 同一 IP 10 分钟内最多输错 10 次管理员令牌
