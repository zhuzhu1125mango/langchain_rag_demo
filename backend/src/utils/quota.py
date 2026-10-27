"""聊天/上传资源配额（SEC-2）。

计数优先走已初始化的 Redis（多 worker/多副本一致）；未初始化时：
- 开发/测试：降级为进程内计数，避免阻断本地使用；
- 生产：抛 QuotaUnavailable（fail-closed），防止 Redis 故障时配额失效。

聊天配额包含三层：每分钟速率、每日消息数、并发流数。上传配额为每日字节数。
"""

import logging
import time
from datetime import datetime, timezone
from typing import Dict, Tuple

from fastapi import HTTPException, status

from src.config import settings
from src.utils.rate_limit import check_rate_limit_async, get_redis_client_if_initialized

logger = logging.getLogger("rag_system")

# 进程内降级计数：key -> (窗口到期时间戳, 当前值)
_memory_counters: Dict[str, Tuple[float, int]] = {}
_memory_concurrency: Dict[str, int] = {}

_COUNTER_LUA = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local amount = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
if current + amount > limit then
  return 0
end
local new_value = redis.call('INCRBY', KEYS[1], amount)
if new_value == amount then
  redis.call('EXPIRE', KEYS[1], ttl)
end
return 1
"""

_ACQUIRE_CONCURRENCY_LUA = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local limit = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
if current >= limit then
  return 0
end
redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ttl)
return 1
"""

_RELEASE_CONCURRENCY_LUA = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current > 0 then
  redis.call('DECR', KEYS[1])
end
return 1
"""


class QuotaUnavailable(RuntimeError):
    """配额后端不可用（生产 fail-closed）。"""


def _enabled() -> bool:
    return bool(getattr(settings.security, "QUOTA_ENFORCEMENT_ENABLED", True))


def reset_memory_quotas() -> None:
    """清空进程内配额计数（测试隔离用）。"""
    _memory_counters.clear()
    _memory_concurrency.clear()


def _raise_quota(scope: str, retry_after: int = 60) -> None:
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=f"{scope}配额已用尽，请稍后再试",
        headers={"Retry-After": str(retry_after)},
    )


def _seconds_until_utc_midnight() -> int:
    now = datetime.now(timezone.utc)
    seconds = 86400 - (now.hour * 3600 + now.minute * 60 + now.second)
    return max(60, seconds + 300)


def _date_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _memory_counter(key: str, amount: int, limit: int, ttl: int) -> bool:
    now = time.monotonic()
    expires, current = _memory_counters.get(key, (now + ttl, 0))
    if now >= expires:
        expires, current = now + ttl, 0
    if current + amount > limit:
        _memory_counters[key] = (expires, current)
        return False
    _memory_counters[key] = (expires, current + amount)
    return True


async def _consume_counter(key: str, amount: int, limit: int, ttl: int) -> bool:
    if amount <= 0 or limit <= 0:
        return True
    client = get_redis_client_if_initialized()
    if client is not None:
        try:
            result = await client.eval(_COUNTER_LUA, 1, key, amount, limit, ttl)
            return bool(int(result))
        except Exception as exc:
            logger.warning("Redis 配额计数失败: %s", exc)
            if settings.IS_PRODUCTION:
                raise QuotaUnavailable("配额后端不可用") from exc
    elif settings.IS_PRODUCTION:
        raise QuotaUnavailable("配额后端不可用（Redis 未初始化）")

    return _memory_counter(key, amount, limit, ttl)


async def acquire_chat_slot(user_id: str) -> None:
    """获取一次聊天配额（速率 + 日额度 + 并发）。失败抛 429/503。"""
    if not _enabled():
        return

    sec = settings.security
    concurrent_key = f"rag:quota:chat:concurrent:{user_id}"
    if not await _acquire_concurrency(
        concurrent_key, sec.CHAT_MAX_CONCURRENT_STREAMS_PER_USER, ttl=1800
    ):
        _raise_quota("聊天并发", retry_after=30)

    try:
        if not await check_rate_limit_async(
            f"rag:quota:chat:rpm:{user_id}", sec.CHAT_RATE_LIMIT_PER_MINUTE, 60
        ):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="聊天请求过于频繁，请稍后再试",
                headers={"Retry-After": "60"},
            )
        daily_key = f"rag:quota:chat:daily:{_date_key()}:{user_id}"
        if not await _consume_counter(
            daily_key, 1, sec.CHAT_DAILY_MESSAGE_QUOTA, _seconds_until_utc_midnight()
        ):
            _raise_quota("聊天每日消息")
    except Exception:
        await release_chat_slot(user_id)
        raise


async def release_chat_slot(user_id: str) -> None:
    """释放聊天并发槽（幂等；失败仅记录）。"""
    if not _enabled():
        return
    key = f"rag:quota:chat:concurrent:{user_id}"
    client = get_redis_client_if_initialized()
    if client is not None:
        try:
            await client.eval(_RELEASE_CONCURRENCY_LUA, 1, key)
            return
        except Exception as exc:
            logger.warning("Redis 释放聊天并发槽失败: %s", exc)
    current = _memory_concurrency.get(key, 0)
    _memory_concurrency[key] = max(0, current - 1)


async def _acquire_concurrency(key: str, limit: int, ttl: int) -> bool:
    if limit <= 0:
        return True
    client = get_redis_client_if_initialized()
    if client is not None:
        try:
            result = await client.eval(_ACQUIRE_CONCURRENCY_LUA, 1, key, limit, ttl)
            return bool(int(result))
        except Exception as exc:
            logger.warning("Redis 聊天并发计数失败: %s", exc)
            if settings.IS_PRODUCTION:
                raise QuotaUnavailable("配额后端不可用") from exc
    elif settings.IS_PRODUCTION:
        raise QuotaUnavailable("配额后端不可用（Redis 未初始化）")

    current = _memory_concurrency.get(key, 0)
    if current >= limit:
        return False
    _memory_concurrency[key] = current + 1
    return True


async def consume_upload_quota(user_id: str, size_bytes: int) -> bool:
    """消耗上传日配额；超出返回 False（由调用方决定 429）。"""
    if not _enabled() or size_bytes <= 0:
        return True
    limit_bytes = settings.security.UPLOAD_DAILY_MB_PER_USER * 1024 * 1024
    key = f"rag:quota:upload:daily:{_date_key()}:{user_id}"
    return await _consume_counter(key, size_bytes, limit_bytes, _seconds_until_utc_midnight())


async def refund_upload_quota(user_id: str, size_bytes: int) -> None:
    """上传失败时退回日配额（best-effort，不影响主流程）。"""
    if not _enabled() or size_bytes <= 0:
        return
    key = f"rag:quota:upload:daily:{_date_key()}:{user_id}"
    client = get_redis_client_if_initialized()
    if client is not None:
        try:
            await client.decrby(key, size_bytes)
            return
        except Exception as exc:
            logger.warning("Redis 退回上传配额失败: %s", exc)
    _memory_counters[key] = (
        _memory_counters.get(key, (time.monotonic() + 86400, 0))[0],
        max(0, _memory_counters.get(key, (0, 0))[1] - size_bytes),
    )


async def consume_eval_quota(user_id: str, units: int = 1) -> None:
    """消耗评估 API 配额（SEC-17）：每分钟请求数 + 每日样本单元数。

    评估会触发 embedding/LLM 调用，按用户限流避免单个账号反复刷评估接口
    拖垮共享资源。``units`` 为本次请求的样本数（单条 = 1），
    超出任一层返回 429。
    """
    if not _enabled() or units <= 0:
        return

    sec = settings.security
    if not await check_rate_limit_async(
        f"rag:quota:eval:rpm:{user_id}", sec.EVAL_RATE_LIMIT_PER_MINUTE, 60
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="评估请求过于频繁，请稍后再试",
            headers={"Retry-After": "60"},
        )

    daily_key = f"rag:quota:eval:daily:{_date_key()}:{user_id}"
    if not await _consume_counter(
        daily_key, units, sec.EVAL_DAILY_QUOTA, _seconds_until_utc_midnight()
    ):
        _raise_quota("评估每日")


def eval_batch_limit() -> int:
    """批量评估单次样本上限（SEC-17；<=0 表示不限）。"""
    return max(0, int(getattr(settings.security, "EVAL_BATCH_MAX_SAMPLES", 100) or 0))
