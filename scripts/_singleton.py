"""单实例锁：防止消耗配额的脚本被重复启动。

起因是一次真实事故——选址脚本被误启动 5 个实例并行运行 18 分钟，
聚合速率达到单实例限速的 5 倍，直接烧光了地点检索的当日配额，
同时也污染了并发与延迟的测量结果（详见 docs/api-optimization.md 第 4.1 节）。

对配额受限的 API 来说，重复启动的代价是不可逆的：配额要等次日 0 点才重置。
因此凡是会大量调用外部 API 的脚本，都应当持有单实例锁。
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from pathlib import Path


def _pid_alive(pid: int) -> bool:
    """判断进程是否仍在运行。锁文件可能因崩溃或强杀而残留，需要甄别。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return False
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError) as exc:
        return isinstance(exc, PermissionError)  # 权限不足说明进程确实存在
    return True


class AlreadyRunning(RuntimeError):
    pass


@contextlib.contextmanager
def single_instance(name: str, lock_dir: Path | None = None) -> Iterator[None]:
    """确保同名脚本同时只有一个实例在跑。

    已有存活实例时抛 AlreadyRunning；锁文件属于已退出的进程则视为陈旧锁，直接接管。
    """
    directory = lock_dir or Path(__file__).resolve().parent.parent / ".cache" / "locks"
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / f"{name}.lock"

    if lock_path.exists():
        try:
            other = int(lock_path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            other = -1
        if _pid_alive(other):
            raise AlreadyRunning(
                f"脚本 {name} 已有实例在运行（PID {other}）。\n"
                f"并行运行会成倍消耗 API 日配额且污染测量结果。\n"
                f"请等待其结束，或确认其已死亡后删除 {lock_path}"
            )
        lock_path.unlink(missing_ok=True)  # 陈旧锁，接管

    lock_path.write_text(str(os.getpid()), encoding="utf-8")
    try:
        yield
    finally:
        lock_path.unlink(missing_ok=True)
