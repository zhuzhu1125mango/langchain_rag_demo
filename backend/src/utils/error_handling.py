"""统一异常分级与结构化上下文（CODE-2 样板）。

仓库里 `except Exception` 长期偏多，且各处以不同文案、不同级别记录，导致
「可恢复的上游抖动」和「必须修的编程错误」混在同一个 error 日志里，既淹没信号
也无法据此决定是否重试。

本模块把异常分成三级，并提供统一记录入口：

- ``RECOVERABLE``（可恢复）：网络抖动、超时、连接被拒、上游限流/不可用。
  调用方可以降级或重试；记录为 **warning**，不输出堆栈（避免刷日志）。
- ``REPORTABLE``（需上报）：未知业务异常、数据不一致、外部返回不可解析等。
  需要人排查；记录为 **error** 并附堆栈。
- ``PROGRAMMING``（编程错误）：类型/属性/断言/键索引等本不该发生的错误，
  说明代码有缺陷，应修复而不是重试；记录为 **error** 并附堆栈。

使用示例（路由层）::

    except Exception as exc:
        log_exception(logger, exc, action="document.classify", doc_id=doc_id)
        raise HTTPException(status_code=500, detail="文档分类失败，请稍后重试")

服务层可据 ``is_recoverable`` 决定是否重试/降级。
"""

import logging
from enum import Enum
from typing import Any, Optional


class ErrorSeverity(str, Enum):
    """异常严重级（决定日志级别、是否重试、是否需要人介入）。"""

    RECOVERABLE = "recoverable"
    REPORTABLE = "reportable"
    PROGRAMMING = "programming"


# 编程错误：出现即代表代码缺陷，重试无意义
_PROGRAMMING_TYPES: tuple = (
    TypeError,
    AttributeError,
    AssertionError,
    IndexError,
    KeyError,
    NameError,
    UnboundLocalError,
    ZeroDivisionError,
    NotImplementedError,
)

# 可恢复：超时 / 连接类（Python 内建）
_RECOVERABLE_TYPES: tuple = (
    TimeoutError,  # 3.11+ 起 asyncio.TimeoutError 即此类型
    ConnectionError,  # 含 ConnectionResetError / ConnectionRefusedError
    ConnectionAbortedError,
    ConnectionRefusedError,
    ConnectionResetError,
    BrokenPipeError,
    InterruptedError,
)

# 可恢复：第三方客户端的瞬时故障，按模块前缀与类名识别（避免为分类引入重依赖）
_RECOVERABLE_MODULE_PREFIXES: tuple = (
    "httpx",
    "httpcore",
    "redis",
    "pymilvus",
    "aiolimiter",
    "aiohttp",
    "urllib3",
    "sqlalchemy.exc",  # OperationalError/DBAPIError 等连接类
)

# 类名特征（仅匹配类名，避免把 "connection" 出现在业务文案里的 ValueError 误判）
_RECOVERABLE_NAME_HINTS: tuple = (
    "timeout",
    "connect",
    "connection",
    "ratelimit",
    "rate_limit",
    "throttl",
    "pool",
    "unavailable",
)

# 文案特征（只在消息里出现这些强信号时才判可恢复）
_RECOVERABLE_MESSAGE_HINTS: tuple = (
    "rate limit",
    "ratelimiter",
    "too many requests",
    "temporarily unavailable",
    "connection refused",
    "connection reset",
    "connection aborted",
    "timed out",
    "timeout",
)

# 明确「不是可恢复」的常见名字（避免 "connection" 前缀把编程错误误判）
_PROGRAMMING_NAME_HINTS: tuple = (
    "AttributeError",
    "TypeError",
    "KeyError",
    "IndexError",
    "AssertionError",
    "InvalidArgument",  # 参数非法属于调用方缺陷
)


def _module_of(exc: BaseException) -> str:
    return (type(exc).__module__ or "").lower()


def _name_of(exc: BaseException) -> str:
    return type(exc).__name__


def _message_of(exc: BaseException) -> str:
    try:
        return str(exc).lower()
    except Exception:  # 异常对象的 __str__ 自身可能抛错，分类不能因此失败
        return ""


def classify_exception(exc: BaseException) -> ErrorSeverity:
    """把异常归类为三级之一（纯函数，便于单测与复用）。

    判定顺序：编程错误 → 可恢复（内建类型 / 模块前缀 / 名字特征）→ 需上报。
    """
    if isinstance(exc, _PROGRAMMING_TYPES):
        return ErrorSeverity.PROGRAMMING

    name = _name_of(exc)
    if any(hint in name for hint in _PROGRAMMING_NAME_HINTS):
        return ErrorSeverity.PROGRAMMING

    if isinstance(exc, _RECOVERABLE_TYPES):
        return ErrorSeverity.RECOVERABLE

    if _module_of(exc).startswith(_RECOVERABLE_MODULE_PREFIXES):
        return ErrorSeverity.RECOVERABLE

    name = _name_of(exc)
    if any(hint in name.lower() for hint in _RECOVERABLE_NAME_HINTS):
        return ErrorSeverity.RECOVERABLE
    if any(hint in _message_of(exc) for hint in _RECOVERABLE_MESSAGE_HINTS):
        return ErrorSeverity.RECOVERABLE

    return ErrorSeverity.REPORTABLE


def is_recoverable(exc: BaseException) -> bool:
    """是否可以安全重试/降级（供调用方决定策略）。"""
    return classify_exception(exc) is ErrorSeverity.RECOVERABLE


def _level_for(severity: ErrorSeverity) -> int:
    """分级 → 日志级别：可恢复记 warning（不刷堆栈），其余记 error。"""
    return logging.WARNING if severity is ErrorSeverity.RECOVERABLE else logging.ERROR


def format_context(action: str, severity: ErrorSeverity, exc: BaseException, **context: Any) -> str:
    """渲染统一上下文串：action / severity / error / 业务键值。"""
    parts = [f"action={action}", f"severity={severity.value}", f"error={_name_of(exc)}"]
    for key, value in context.items():
        if value is None:
            continue
        parts.append(f"{key}={value}")
    return " ".join(parts)


def log_exception(
    logger: logging.Logger,
    exc: BaseException,
    *,
    action: str,
    severity: Optional[ErrorSeverity] = None,
    message: Optional[str] = None,
    **context: Any,
) -> ErrorSeverity:
    """按分级记录异常并返回该分级（方便调用方继续分支）。

    Args:
        logger: 目标 logger。
        exc: 捕获到的异常。
        action: 稳定动作标识（如 ``document.classify``），用于检索聚合。
        severity: 显式指定分级；缺省按 :func:`classify_exception` 判定。
        message: 人类可读前缀；缺省为「操作失败」。
        **context: 附加业务键值（doc_id / kb_id / stage 等），None 自动跳过。

    Returns:
        实际采用的 :class:`ErrorSeverity`。
    """
    resolved = severity or classify_exception(exc)
    text = f"{message or '操作失败'} [{format_context(action, resolved, exc, **context)}]"
    if resolved is ErrorSeverity.RECOVERABLE:
        # 可恢复：保留错误文本但不输出堆栈，避免网络抖动刷满日志
        logger.warning("%s: %s", text, exc)
    else:
        logger.error("%s: %s", text, exc, exc_info=True)
    return resolved
