"""进程内滑动窗口限流（W2-9）。

用于认证接口（/auth/login、/auth/register）的暴力破解与滥刷防护。
单机自托管形态下使用进程内存桶即可，不引入 slowapi 等外部依赖；
多副本部署时各副本独立计数（限流阈值按副本数放大即可）。
"""

import time
from collections import defaultdict, deque
from typing import Deque, Dict

from fastapi import HTTPException, Request, status

from src.config import settings

# key = "{scope}:{client_ip}" -> 窗口内请求时间戳
_buckets: Dict[str, Deque[float]] = defaultdict(deque)


def check_rate_limit(key: str, limit: int, window_seconds: float = 60.0) -> bool:
    """滑动窗口计数：窗口内请求数达到 limit 时返回 False（拒绝）。

    Args:
        key: 限流键（scope + 客户端标识）。
        limit: 窗口内允许的最大请求数。
        window_seconds: 窗口时长（秒）。

    Returns:
        bool: True = 放行（本次已计入），False = 超限拒绝（不计入）。
    """
    now = time.monotonic()
    bucket = _buckets[key]
    while bucket and now - bucket[0] > window_seconds:
        bucket.popleft()
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


def reset_rate_limits() -> None:
    """清空所有限流桶（测试隔离用）。"""
    _buckets.clear()


def enforce_auth_rate_limit(request: Request, scope: str, limit: int, window_seconds: float = 60.0) -> None:
    """认证接口限流入口：超限抛 429。

    配置在请求时读取（模块导入时求值会导致测试 monkeypatch 失效）。

    Args:
        request: FastAPI 请求对象（取客户端 IP）。
        scope: 限流域（如 "login" / "register"）。
        limit: 窗口内允许的最大请求数；<=0 表示不限流。
        window_seconds: 窗口时长（秒）。
    """
    if limit <= 0:
        return
    client_ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(f"{scope}:{client_ip}", limit, window_seconds):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="请求过于频繁，请稍后再试",
            headers={"Retry-After": str(int(window_seconds))},
        )


def get_auth_rate_limits() -> tuple:
    """读取认证接口限流阈值（请求时求值，便于测试覆盖）。"""
    return (
        getattr(settings, "AUTH_RATE_LIMIT_LOGIN", None)
        or getattr(settings.security, "AUTH_RATE_LIMIT_LOGIN", 5),
        getattr(settings, "AUTH_RATE_LIMIT_REGISTER", None)
        or getattr(settings.security, "AUTH_RATE_LIMIT_REGISTER", 3),
    )
