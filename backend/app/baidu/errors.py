"""百度地图 Web 服务 API 的错误码分类。

分类依据是 reports/quota-report.md 第四节的实测结论，核心有两条：

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


def classify(status: int, message: str = "", endpoint: str = "") -> BaiduApiError:
    """把状态码翻译成对应的异常类型，供调用方按类型分流处理。"""
    if status in QUOTA_STATUS:
        return QuotaExhaustedError(status, message, endpoint)
    if status in CONFIG_STATUS:
        return ConfigurationError(status, message, endpoint)
    return BaiduApiError(status, message, endpoint)
