"""百度地图 Web 服务 API 的错误码分类。

分类依据是开发期的实测（docs/api-optimization.md 第 4.1 节），核心有两条：

1. `401/402`（并发超限）是瞬时噪声，退避后可恢复。实测中一次孤立的串行请求
   也会返回 401，与请求合法性无关，因此必须重试而非放弃。
2. `301/302`（配额耗尽）是硬性终止信号，且**按接口独立计算**。实测地点检索
   返回 302 的同一时刻，地理编码与批量算路完全正常。对这类错误重试毫无意义，
   只会白白消耗退避时间，应立即切换缓存或降级模式。

把两者混为一谈是最常见的误区：全部重试会在配额耗尽时空转，
全部终止又会在瞬时抖动时误降级。
"""

from __future__ import annotations

STATUS_MEANING: dict[int, str] = {
    -1: "参数名错误或服务端未识别请求",
    0: "成功",
    1: "服务器内部错误",
    2: "请求参数非法",
    3: "权限校验失败",
    4: "配额校验失败",
    5: "AK 不存在或非法",
    101: "服务未开启",
    102: "不通过白名单或签名错误",
    200: "APP 不存在，AK 有误或已删除",
    210: "APP IP 校验失败",
    211: "APP SN 校验失败",
    220: "APP Referer 校验失败",
    240: "APP 服务被禁用",
    251: "APP 用户被禁用",
    260: "服务不存在",
    261: "服务被禁用",
    301: "永久配额超限，服务已停用",
    302: "当日配额超限，次日 0 点重置",
    401: "瞬时并发超限，可退避重试",
    402: "瞬时并发超限（黑名单）",
}

# 瞬时噪声：退避重试
RETRYABLE_STATUS = frozenset({401, 402, 1})

# 配额耗尽：立即终止该接口的调用，转缓存或降级
QUOTA_STATUS = frozenset({301, 302})

# 配置错误：属于部署问题，终止并告警，重试与降级都无济于事
CONFIG_STATUS = frozenset({200, 210, 211, 220, 240, 251, 260, 261, 5, 102})


def describe(status: int | None) -> str:
    if status is None:
        return "无响应"
    return STATUS_MEANING.get(status, f"未知状态码 {status}")


class BaiduApiError(RuntimeError):
    """百度 API 返回了非零状态码。"""

    def __init__(self, status: int, message: str = "", endpoint: str = "") -> None:
        self.status = status
        self.endpoint = endpoint
        super().__init__(
            f"[{endpoint or 'baidu'}] status={status} {describe(status)}"
            + (f"：{message}" if message else "")
        )


class QuotaExhaustedError(BaiduApiError):
    """接口当日/永久配额耗尽。调用方应转入缓存或降级路径，不要重试。"""


class ConfigurationError(BaiduApiError):
    """AK 无效或白名单未配置。属于部署问题，需人工介入。"""


class IncompleteSamplingError(BaiduApiError):
    """等时圈采样有整批请求失败（重试耗尽、网络中断）。

    这些点是「没测到」而不是「走不到」，当成障碍会画出一个缩水甚至为 0 的圈，
    故整个等时圈不出，并把失败原因带给调用方。
    """

    def __init__(self, failures: list[str], endpoint: str) -> None:
        self.status = -1
        self.endpoint = endpoint
        self.failures = list(failures)
        reasons = "；".join(sorted({f.split(": ", 1)[-1][:80] for f in self.failures}))
        RuntimeError.__init__(
            self, f"等时圈采样有 {len(self.failures)} 批批量算路失败：{reasons[:240]}"
        )


def classify(status: int, message: str = "", endpoint: str = "") -> BaiduApiError:
    """把状态码翻译成对应的异常类型，供调用方按类型分流处理。"""
    if status in QUOTA_STATUS:
        return QuotaExhaustedError(status, message, endpoint)
    if status in CONFIG_STATUS:
        return ConfigurationError(status, message, endpoint)
    return BaiduApiError(status, message, endpoint)


# ---------- 面向用户的说明 ----------
#
# 报错必须说清「哪个服务、为什么、什么时候恢复、现在能做什么」。
# 笼统的「配额耗尽」「AK 无效」会让人去查错方向：配额按服务独立计算，
# 地点检索用完时批量算路可能完全正常；AK 错误也分没填、填错、白名单、服务未勾选好几种。

SERVICE_LABELS: tuple[tuple[str, str], ...] = (
    ("/routematrix/v2/walking", "批量算路（步行）"),
    ("/routematrix/v2/riding", "批量算路（骑行）"),
    ("/routematrix/v2/driving", "批量算路（驾车）"),
    ("/directionlite/v1/walking", "步行路线规划"),
    ("/place/v2/search", "地点检索"),
    ("/geocoding/v3", "地理编码"),
    ("/reverse_geocoding/v3", "逆地理编码"),
    ("/geoconv/v1", "坐标转换"),
)


def service_label(endpoint: str) -> str:
    for prefix, label in SERVICE_LABELS:
        if endpoint.startswith(prefix):
            return label
    return endpoint or "百度地图接口"


# 配置类错误的具体原因与处置
CONFIG_HINTS: dict[int, str] = {
    5: "服务端 AK 不存在或格式不对：请核对 .env 里的 BAIDU_SERVER_AK 是否完整复制",
    102: "白名单或签名校验失败：服务端 AK 的 IP 白名单没有包含本机出口 IP，或开启了 SN 校验",
    200: "服务端 AK 不存在或已被删除：请在百度控制台确认这个 AK 还在",
    210: "IP 白名单校验失败：请在百度控制台把本机出口 IP 加入该 AK 的白名单，或临时填 0.0.0.0/0",
    211: "SN 签名校验失败：该 AK 开启了 SN 校验，本项目不做签名，请在控制台改用 IP 白名单校验",
    220: "Referer 校验失败：后端用了「浏览器端」类型的 AK，服务端请求必须用「服务端」类型的 AK",
    240: "该 AK 没有开通这项服务：请在百度控制台编辑 AK，勾选对应服务",
    251: "百度账号已被禁用，请登录控制台查看原因",
    260: "服务不存在：请求的接口地址有误",
    261: "该 AK 的这项服务已被禁用：请在百度控制台编辑 AK，重新启用对应服务",
}

MISSING_AK_MESSAGE = (
    "未配置服务端 AK（BAIDU_SERVER_AK）：预生成样例、离线模拟与导出可以照常使用，"
    "实时计算、地址搜索、模拟新建的路网核验需要它。"
    "请复制 .env.example 为 .env，填入「服务端」类型的 AK 后重启后端。"
)


def config_hint(exc: BaiduApiError) -> str:
    """配置类错误的可读说明。未配置 AK 的情形单独给出，最常见也最好解决。"""
    if exc.status == 5 and "未配置" in str(exc):
        return MISSING_AK_MESSAGE
    hint = CONFIG_HINTS.get(exc.status, "AK 或白名单配置有误，请检查 .env 与百度控制台设置")
    return f"{service_label(exc.endpoint)}：{hint}（状态码 {exc.status}）"


def quota_hint(exc: BaiduApiError) -> str:
    """配额耗尽的可读说明：哪个服务、永久还是当日、何时恢复。"""
    service = service_label(exc.endpoint)
    if exc.status == 301:
        return (
            f"{service}的永久配额已超限、服务被停用（状态码 301），不会自动恢复，"
            "需要在百度控制台申请提升配额或更换 AK"
        )
    return f"{service}今天的配额已经用完（状态码 302），北京时间次日 0 点自动重置"
