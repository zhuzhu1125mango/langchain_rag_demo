"""认证接口限流与客户端 IP 解析（SEC-3）。

- 客户端 IP：仅当直连 peer 属于受信代理网段时才解析 X-Forwarded-For / X-Real-IP，
  防止公网客户端伪造代理头绕过限流。
- 限流：优先使用已初始化的 Redis 做滑动窗口（多 worker/多副本一致）；Redis
  未初始化或不可用时降级为进程内桶（认证接口可接受短暂降级，但不全局锁死）。

旧的同步 ``check_rate_limit`` 保留为进程内实现，供单测与 Redis 故障降级使用。
"""

import ipaddress
import logging
import time
import uuid
from collections import defaultdict, deque
from typing import Deque, Dict, Optional

from fastapi import HTTPException, Request, status
from starlette.types import Scope

from src.config import settings

logger = logging.getLogger("rag_system")

# key = "{scope}:{client_ip}" -> 窗口内请求时间戳（进程内降级用）
_buckets: Dict[str, Deque[float]] = defaultdict(deque)

# Redis 滑动窗口 Lua：清理过期 → 计数 → 放行并写入唯一 member。
_RATE_LIMIT_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window_ms)
local count = redis.call('ZCARD', key)
if count >= limit then
  return 0
end
redis.call('ZADD', key, now, member)
redis.call('PEXPIRE', key, window_ms + 1000)
return 1
"""


def check_rate_limit(key: str, limit: int, window_seconds: float = 60.0) -> bool:
    """进程内滑动窗口计数（Redis 不可用时的降级实现）。"""
    now = time.monotonic()
    bucket = _buckets[key]
    while bucket and now - bucket[0] > window_seconds:
        bucket.popleft()
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


def reset_rate_limits() -> None:
    """清空所有进程内限流桶（测试隔离用）。"""
    _buckets.clear()


def _trusted_proxy_networks() -> list:
    """解析 TRUSTED_PROXY_CIDRS（逗号分隔，支持 CIDR 与单 IP）。"""
    raw = getattr(settings.security, "TRUSTED_PROXY_CIDRS", "") or ""
    networks = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            logger.warning("忽略非法 TRUSTED_PROXY_CIDRS 项: %s", item)
    return networks


def _is_trusted_proxy(ip: str, networks: Optional[list] = None) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for network in networks if networks is not None else _trusted_proxy_networks():
        if addr in network:
            return True
    return False


def resolve_client_ip(scope: Scope) -> str:
    """从 ASGI scope 解析客户端 IP；只信任受信代理传来的转发头。"""
    client = scope.get("client")
    peer = client[0] if client else ""
    if not peer:
        return "unknown"

    networks = _trusted_proxy_networks()
    if not _is_trusted_proxy(peer, networks):
        # 直连客户端：忽略任何转发头，防止伪造。
        return peer

    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    forwarded = headers.get("x-forwarded-for", "")
    if forwarded:
        hops = [h.strip() for h in forwarded.split(",") if h.strip()]
        # 从右往左找第一个非受信地址（最接近客户端的可信链末端）。
        for hop in reversed(hops):
            if not _is_trusted_proxy(hop, networks):
                return hop
        if hops:
            return hops[0]

    real_ip = headers.get("x-real-ip", "").strip()
    if real_ip:
        return real_ip
    return peer


def get_client_ip(request: Request) -> str:
    """HTTP 请求客户端 IP（兼容直接调用与中间件改写后的 scope）。"""
    return resolve_client_ip(request.scope)


def _redis_client():
    """返回已初始化的 Redis 客户端；未初始化时返回 None（不主动建连）。"""
    try:
        from src.services.cache_service import CacheService

        if not CacheService._initialized.get(CacheService, False):
            return None
        cache = CacheService._instances.get(CacheService)
        if cache is not None and getattr(cache, "available", False):
            return cache.client
    except Exception:
        return None
    return None


def get_redis_client_if_initialized():
    """公开只读入口：供配额等工具复用同一 Redis 客户端，不主动初始化。"""
    return _redis_client()


async def check_rate_limit_async(key: str, limit: int, window_seconds: float = 60.0) -> bool:
    """异步滑动窗口：优先 Redis，失败降级进程内。"""
    if limit <= 0:
        return True

    client = _redis_client()
    if client is not None:
        now_ms = int(time.time() * 1000)
        window_ms = int(window_seconds * 1000)
        try:
            result = await client.eval(
                _RATE_LIMIT_LUA,
                1,
                key,
                now_ms,
                window_ms,
                limit,
                f"{now_ms}:{uuid.uuid4().hex}",
            )
            return bool(int(result))
        except Exception as exc:
            logger.warning("Redis 限流失败，降级进程内桶: %s", exc)

    return check_rate_limit(key, limit, window_seconds)


async def enforce_auth_rate_limit(
    request: Request, scope: str, limit: int, window_seconds: float = 60.0
) -> None:
    """认证接口限流入口：超限抛 429。"""
    if limit <= 0:
        return
    client_ip = get_client_ip(request)
    if not await check_rate_limit_async(f"{scope}:{client_ip}", limit, window_seconds):
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
