"""结构化日志上下文（OPS-3）。

统一为日志注入三个上下文维度，便于采集端按请求/用户/阶段聚合与排障：

- ``request_id``：HTTP/WS 请求中间件生成，贯穿到 services 层日志；
- ``user_id``：认证依赖解析成功后绑定（匿名/未认证为 ``-``）；
- ``stage``：管道阶段装饰器 ``@log_stage`` 在阶段执行期间绑定。

输出格式由 ``settings.logging.LOG_FORMAT`` 决定：``text``（默认，追加
``[rid=.. user=.. stage=..]``）或 ``json``（单行 JSON，供日志平台解析）。
"""

import inspect
import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Dict, Optional

_REQUEST_ID: ContextVar[str] = ContextVar("request_id", default="-")
_USER_ID: ContextVar[str] = ContextVar("user_id", default="-")
_STAGE: ContextVar[str] = ContextVar("stage", default="-")


def bind_request_id(value: Optional[str]) -> Token:
    """绑定当前请求 ID，返回用于复位的 token。"""
    return _REQUEST_ID.set(str(value) if value else "-")


def bind_user(user_id: Optional[str]) -> Token:
    """绑定当前认证用户 ID，返回用于复位的 token。"""
    return _USER_ID.set(str(user_id) if user_id else "-")


def bind_stage(value: Optional[str]) -> Token:
    """绑定当前阶段名，返回用于复位的 token。"""
    return _STAGE.set(str(value) if value else "-")


def reset(token: Token) -> None:
    """复位任意上下文 token（对已失效 token 容错）。"""
    try:
        token.var.reset(token)
    except (ValueError, RuntimeError):
        # token 已复位或来自其他上下文：忽略，避免日志上下文污染主流程
        pass


def current_context() -> Dict[str, str]:
    """当前日志上下文字典（测试与诊断用）。"""
    return {
        "request_id": _REQUEST_ID.get(),
        "user_id": _USER_ID.get(),
        "stage": _STAGE.get(),
    }


@contextmanager
def request_scope(request_id: str):
    """请求级上下文：绑定 request_id 并复位全部维度（中间件使用）。"""
    rid_token = bind_request_id(request_id)
    user_token = bind_user(None)
    stage_token = bind_stage(None)
    try:
        yield
    finally:
        reset(stage_token)
        reset(user_token)
        reset(rid_token)


@contextmanager
def stage_scope(name: str):
    """阶段级上下文：进入时绑定 stage，退出时恢复上一阶段。"""
    token = bind_stage(name)
    try:
        yield
    finally:
        reset(token)


class ContextLogFilter(logging.Filter):
    """把 request_id / user_id / stage 注入每条 LogRecord。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _REQUEST_ID.get()
        record.user_id = _USER_ID.get()
        record.stage = _STAGE.get()
        return True


class JsonLogFormatter(logging.Formatter):
    """单行 JSON 日志格式（LOG_FORMAT=json 时启用）。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "user_id": getattr(record, "user_id", "-"),
            "stage": getattr(record, "stage", "-"),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


TEXT_LOG_FORMAT = (
    "%(asctime)s - %(name)s - %(levelname)s "
    "[rid=%(request_id)s user=%(user_id)s stage=%(stage)s] - %(message)s"
)


def build_formatter(log_format: str = "text") -> logging.Formatter:
    """按配置构造日志 formatter（未知值回退 text）。"""
    return JsonLogFormatter() if str(log_format or "").lower() == "json" else logging.Formatter(
        TEXT_LOG_FORMAT
    )


def log_stage(name: str):
    """装饰器：为 async/sync/async-generator 函数执行期间绑定日志 stage。"""

    def decorator(func):
        if inspect.isasyncgenfunction(func):
            @wraps(func)
            async def async_gen_wrapper(*args: Any, **kwargs: Any):
                with stage_scope(name):
                    async for item in func(*args, **kwargs):
                        yield item

            return async_gen_wrapper

        if inspect.iscoroutinefunction(func):
            @wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any):
                with stage_scope(name):
                    return await func(*args, **kwargs)

            return async_wrapper

        @wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any):
            with stage_scope(name):
                return func(*args, **kwargs)

        return sync_wrapper

    return decorator
