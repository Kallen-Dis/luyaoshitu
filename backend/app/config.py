"""运行时配置。

所有参数来自项目根目录的 .env；默认值取自 reports/quota-report.md 的实测结论。
刻意不引入 pydantic-settings 之类的依赖，保持配置层足够薄、可读。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / ".env"


def _load_env_file(path: Path) -> None:
    """把 .env 读进 os.environ，已存在的环境变量优先（便于容器部署时覆盖）。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class Settings:
    server_ak: str
    browser_ak: str

    # 并发与重试：实测地理编码 12 并发安全、16 触发限流，故全局取 8 留余量
    max_qps: int = 8
    http_timeout: float = 30.0
    max_retries: int = 3
    retry_backoff: float = 0.5

    # 批量算路单次终点数上限，实测硬上限 100（101 起返回 status=2 参数非法）
    matrix_batch_size: int = 100

    # 缓存坐标量化网格边长（米）。相近坐标共用缓存，显著节省地点检索的日配额。
    cache_grid_m: float = 50.0
    cache_dir: Path = PROJECT_ROOT / ".cache"

    def require_server_ak(self) -> str:
        if not self.server_ak or self.server_ak.startswith("your_"):
            raise RuntimeError(
                "未配置 BAIDU_SERVER_AK。请复制 .env.example 为 .env 并填入服务端 AK。"
            )
        return self.server_ak


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_env_file(ENV_PATH)
    return Settings(
        server_ak=os.environ.get("BAIDU_SERVER_AK", ""),
        browser_ak=os.environ.get("VITE_BAIDU_BROWSER_AK", ""),
        max_qps=int(os.environ.get("BAIDU_MAX_QPS", 8)),
        http_timeout=float(os.environ.get("BAIDU_HTTP_TIMEOUT", 30)),
        max_retries=int(os.environ.get("BAIDU_MAX_RETRIES", 3)),
        retry_backoff=float(os.environ.get("BAIDU_RETRY_BACKOFF", 0.5)),
        matrix_batch_size=int(os.environ.get("BAIDU_MATRIX_BATCH_SIZE", 100)),
    )
