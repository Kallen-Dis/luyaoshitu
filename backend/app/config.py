"""运行时配置。

所有参数来自项目根目录的 .env；默认值取自实测：批量算路形状、单次往返与并发上限见
docs/api-optimization.md 第 4.1 节，在途数的取舍见 reports/api-benchmark.md。
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
    """把 .env 读进 os.environ，已存在的环境变量优先（便于容器部署时覆盖）。

    容错两种常见写法：Windows 记事本存出的 UTF-8 BOM（否则第一行的键名会带上
    不可见的 \\ufeff 而读不到），以及值两侧的引号（KEY="xxx"）。行尾 # 注释也去掉。
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key.strip(), value)


@dataclass(frozen=True)
class Settings:
    server_ak: str
    browser_ak: str

    # 并发与重试：实测地理编码 12 并发安全、16 触发限流，故全局取 8 留余量
    max_qps: int = 8
    http_timeout: float = 30.0
    max_retries: int = 3
    retry_backoff: float = 0.5

    # 单次完整分析的总时长上限（秒）。批量算路 3 个在途 + 令牌桶限速下，等时圈 + 路线校正
    # + 覆盖 + 圈内网格盲区缓存为空时通常一两分钟；超时即终止，避免请求无限挂起。
    analysis_timeout_s: float = 240.0

    # 配置分块上限，客户端再取出行方式上限的较小值（步行/骑行 50、驾车 100）。
    matrix_batch_size: int = 100

    # 缓存坐标量化网格边长（米）。相近坐标共用缓存，显著节省地点检索的日配额。
    cache_grid_m: float = 50.0
    cache_dir: Path = PROJECT_ROOT / ".cache"

    # 缓存有效期（秒）。步行路网、路线与 POI 变化慢，默认 30 天。
    cache_ttl_s: float = 30 * 86400.0

    # 驾车实时路况的新鲜期（秒），默认 10 分钟。过期条目不删除：
    # 接口失败时拿来兜底，并在结果里写明路况是多久以前的。
    traffic_ttl_s: float = 600.0

    # 步行路线规划（directionlite）同时在途的请求数。实测步行路线 8 并发安全、12 触发 401。
    route_concurrency: int = 4

    # 批量算路同时在途的请求数。总速率仍由令牌桶（max_qps）封顶；
    # 在途数只决定「等网络往返」时能否并行。1 为完全串行。
    matrix_concurrency: int = 3

    # 百度地图 Agent Plan 的 Token（sk-ap- 开头，可选）。只用于「AI 二次核对」：
    # 用语义地点检索再找一遍灰色区域附近的关键设施。只在后端使用，不下发前端
    agent_plan_token: str = ""

    # 用户共享标注：数据库与照片目录（不放 .cache/，那里可以随手清掉）；
    # ADMIN_TOKEN 不配时审核接口关闭；MARKING_SALT 不配时首次运行生成并存在数据目录里
    markings_dir: Path = PROJECT_ROOT / "data" / "user"
    admin_token: str = ""
    marking_salt: str = ""

    trip_day_pairs: int = 500
    trip_hour_pairs: int = 120
    trip_request_pairs: int = 120
    trip_hour_routes: int = 200
    trip_hour_requests: int = 120
    trip_request_routes: int = 12
    trip_day_pois: int = 200
    trip_hour_pois: int = 60
    trip_request_pois: int = 48
    trip_slack_m: float = 30.0
    trip_memory_entries: int = 4096
    trip_search_expansions: int = 20000

    @property
    def admin_enabled(self) -> bool:
        return len(self.admin_token) >= 12 and not self.admin_token.startswith("your_")

    @property
    def agent_plan_configured(self) -> bool:
        token = self.agent_plan_token
        return bool(token) and not token.startswith("your_")

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
        max_qps=int(os.environ.get("BAIDU_MAX_QPS", "8")),
        http_timeout=float(os.environ.get("BAIDU_HTTP_TIMEOUT", "30")),
        max_retries=int(os.environ.get("BAIDU_MAX_RETRIES", "3")),
        retry_backoff=float(os.environ.get("BAIDU_RETRY_BACKOFF", "0.5")),
        matrix_batch_size=int(os.environ.get("BAIDU_MATRIX_BATCH_SIZE", "100")),
        analysis_timeout_s=float(os.environ.get("BAIDU_ANALYSIS_TIMEOUT", "240")),
        cache_ttl_s=float(os.environ.get("BAIDU_CACHE_TTL_DAYS", "30")) * 86400.0,
        traffic_ttl_s=float(os.environ.get("BAIDU_TRAFFIC_TTL_MIN", "10")) * 60.0,
        matrix_concurrency=int(os.environ.get("BAIDU_MATRIX_CONCURRENCY", "3")),
        route_concurrency=int(os.environ.get("BAIDU_ROUTE_CONCURRENCY", "4")),
        agent_plan_token=os.environ.get("BAIDU_MAP_AUTH_TOKEN", "").strip(),
        markings_dir=Path(os.environ["MARKINGS_DIR"])
        if os.environ.get("MARKINGS_DIR")
        else PROJECT_ROOT / "data" / "user",
        admin_token=os.environ.get("ADMIN_TOKEN", ""),
        marking_salt=os.environ.get("MARKING_SALT", ""),
        trip_day_pairs=max(0, int(os.environ.get("TRIP_DAY_PAIRS", "500"))),
        trip_hour_pairs=max(0, int(os.environ.get("TRIP_HOUR_PAIRS", "120"))),
        trip_request_pairs=max(0, int(os.environ.get("TRIP_REQUEST_PAIRS", "120"))),
        trip_hour_routes=max(0, int(os.environ.get("TRIP_HOUR_ROUTES", "200"))),
        trip_hour_requests=max(0, int(os.environ.get("TRIP_HOUR_REQUESTS", "120"))),
        trip_request_routes=max(0, int(os.environ.get("TRIP_REQUEST_ROUTES", "12"))),
        trip_day_pois=max(0, int(os.environ.get("TRIP_DAY_POIS", "200"))),
        trip_hour_pois=max(0, int(os.environ.get("TRIP_HOUR_POIS", "60"))),
        trip_request_pois=max(0, int(os.environ.get("TRIP_REQUEST_POIS", "48"))),
        trip_slack_m=max(0.0, float(os.environ.get("TRIP_SLACK_M", "30"))),
        trip_memory_entries=max(1, int(os.environ.get("TRIP_MEMORY_ENTRIES", "4096"))),
        trip_search_expansions=max(1, int(os.environ.get("TRIP_SEARCH_EXPANSIONS", "20000"))),
    )
