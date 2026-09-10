"""Wiki KB 互斥锁（P5）：编译 / 级联清理 / 去抖调度共用。

双层锁设计：
1. 进程内 asyncio.Lock（get_kb_lock）：单 worker 互斥，行为与 Phase 1 一致
2. 可选 Redis 锁（WIKI_DISTRIBUTED_LOCK 开启时）：多副本 / 多 worker 部署互斥，
   SET NX PX + token，释放走 Lua compare-token-del（只删自己的锁）

降级语义：Redis 不可用（CacheService 未就绪 / 操作抛错）→ 记 warning 与指标后
仅持进程内锁继续（单副本仍然安全，不阻断编译 / 级联主流程）；获取超时抛
TimeoutError，与现状「锁等待计入外层编译超时」的语义一致。
"""

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager

from src.config import settings

logger = logging.getLogger("wiki_lock")

# Redis 锁 key 前缀（per-KB）
LOCK_KEY_PREFIX = "wiki:lock:"
# 锁轮询间隔（秒）
LOCK_POLL_INTERVAL = 0.5
# 锁 TTL 在编译外层超时之上的余量（秒），防业务完成前锁过期
LOCK_TTL_MARGIN_SECONDS = 60

# 释放 Lua：token 匹配才删除，防止误删他人锁（TTL 过期后被他人重新获取的场景）
_RELEASE_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


def get_kb_lock(kb_id: str) -> asyncio.Lock:
    """取（或建）指定知识库的进程内编译互斥锁（双层锁的内层）。"""
    if not hasattr(get_kb_lock, "_locks"):
        get_kb_lock._locks = {}
    key = str(kb_id)
    if key not in get_kb_lock._locks:
        get_kb_lock._locks[key] = asyncio.Lock()
    return get_kb_lock._locks[key]


@asynccontextmanager
async def kb_wiki_lock(kb_id: str):
    """KB 编译互斥双层锁：进程内锁 → Redis 锁（可选），释放顺序相反。

    WIKI_DISTRIBUTED_LOCK=false 时行为与原 get_kb_lock 完全一致（零额外开销）。
    """
    from src.middleware.prometheus import record_wiki_lock

    async with get_kb_lock(kb_id):
        token = None
        client = None
        if settings.wiki_compile.WIKI_DISTRIBUTED_LOCK:
            try:
                client = await _get_redis_client()
            except Exception as e:
                logger.warning(f"Redis 客户端获取失败，分布式锁降级为进程内锁: {e}")
                record_wiki_lock(result="redis_unavailable")
                client = None
            if client is not None:
                try:
                    token = await _acquire_redis_lock(client, str(kb_id))
                except asyncio.TimeoutError:
                    # 获取超时：向上抛出（编译场景由外层 wait_for 兜底语义一致）
                    raise
                except Exception as e:
                    logger.warning(f"Redis 分布式锁不可用，降级为进程内锁: {e}")
                    record_wiki_lock(result="redis_unavailable")
                    token, client = None, None
        else:
            record_wiki_lock(result="ok")
        try:
            yield
        finally:
            if token and client is not None:
                try:
                    await client.eval(_RELEASE_LUA, 1, LOCK_KEY_PREFIX + str(kb_id), token)
                except Exception as e:
                    # 释放失败只留锁至 TTL 过期，不影响业务结果
                    logger.warning(f"Redis 分布式锁释放失败（等 TTL 过期）: kb={kb_id}: {e}")


async def _get_redis_client():
    """复用 CacheService 的 Redis 客户端；不可用时抛异常（由调用方降级）。"""
    from src.services.cache_service import CacheService

    cache = await CacheService.get_instance()
    if not cache.available:
        raise ConnectionError("CacheService 不可用")
    return cache.client


async def _acquire_redis_lock(client, kb_id: str) -> str:
    """轮询获取 Redis 锁；成功返回 token，超时抛 TimeoutError，操作失败抛 ConnectionError。"""
    from src.middleware.prometheus import record_wiki_lock

    token = uuid.uuid4().hex
    ttl_ms = (
        settings.wiki_compile.WIKI_COMPILE_TIMEOUT_SECONDS + LOCK_TTL_MARGIN_SECONDS
    ) * 1000
    deadline = time.monotonic() + settings.wiki_compile.WIKI_COMPILE_TIMEOUT_SECONDS
    key = LOCK_KEY_PREFIX + kb_id
    while True:
        try:
            acquired = await client.set(key, token, nx=True, px=ttl_ms)
        except Exception as e:
            raise ConnectionError(f"Redis 锁操作失败: {e}") from e
        if acquired:
            record_wiki_lock(result="ok")
            return token
        if time.monotonic() >= deadline:
            record_wiki_lock(result="timeout")
            raise asyncio.TimeoutError(f"Redis 分布式锁获取超时: {key}")
        await asyncio.sleep(LOCK_POLL_INTERVAL)
