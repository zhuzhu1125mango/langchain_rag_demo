"""Redis 缓存服务。

为搜索结果、网页正文、知识库列表等提供统一的缓存读写接口，自动处理 JSON 序列化。
当 Redis 不可用时，所有读写操作都会快速失败并记录警告，不会阻塞业务接口。
"""

import asyncio
import json
import logging
from datetime import timedelta
from typing import Any, Optional

import redis.asyncio as aioredis

from src.config import settings
from src.utils.async_singleton import AsyncSingleton

logger = logging.getLogger(__name__)

# Redis 连接与操作超时（秒）。
# 连接超时设置较短，避免 Redis 不可用时长时间阻塞事件循环；
# 操作超时略长，给正常网络抖动留一点余量。
REDIS_SOCKET_CONNECT_TIMEOUT = 2.0
REDIS_SOCKET_TIMEOUT = 2.0
REDIS_HEALTH_CHECK_INTERVAL = 30

# 初始化时 ping 验证的超时时间，必须大于连接超时，否则无法区分连接失败和 ping 超时。
REDIS_INIT_PING_TIMEOUT = 3.0

# 外部调用缓存操作时的兜底超时，防止任何异常情况下缓存操作挂死。
REDIS_OPERATION_TIMEOUT = 3.0


class CacheService(AsyncSingleton["CacheService"]):
    """Redis 客户端封装，支持单例访问与常用缓存操作。

    设计要点：
    1. 初始化阶段通过短超时 ping 验证 Redis 可达性，连接失败立即抛出，
       避免 AsyncSingleton 锁被长时间占用，导致其他请求排队。
    2. 所有缓存操作内置超时和异常捕获，Redis 不可用时不阻塞业务逻辑。
    3. 提供 ``available`` 属性，业务代码可在必要时快速判断缓存状态。
    """

    def __init__(self):
        self.client = None
        self._available = False

    async def _async_init(self):
        """初始化异步 Redis 连接，并立即验证连通性。"""
        password = settings.redis.REDIS_PASSWORD
        if not password:
            raise ValueError(
                "REDIS_PASSWORD 未配置。Redis 密码为必填项，请在 .env 中设置 REDIS_PASSWORD。"
            )

        # 先清理可能存在的旧客户端，避免重复初始化时残留坏连接
        if self.client is not None:
            try:
                await self.client.aclose()
            except Exception:
                pass
            self.client = None

        self.client = aioredis.Redis(
            host=settings.redis.REDIS_HOST,
            port=settings.redis.REDIS_PORT,
            db=settings.redis.REDIS_DB,
            password=password,
            decode_responses=True,
            protocol=2,
            socket_connect_timeout=REDIS_SOCKET_CONNECT_TIMEOUT,
            socket_timeout=REDIS_SOCKET_TIMEOUT,
            health_check_interval=REDIS_HEALTH_CHECK_INTERVAL,
            retry_on_timeout=False,
        )

        try:
            await asyncio.wait_for(self.client.ping(), timeout=REDIS_INIT_PING_TIMEOUT)
            self._available = True
            logger.info("Redis 缓存服务连接成功")
        except asyncio.TimeoutError as exc:
            self._available = False
            await self._close_client_safely()
            raise ConnectionError(
                f"Redis 连接超时（{REDIS_INIT_PING_TIMEOUT}s），"
                f"请检查 REDIS_HOST={settings.redis.REDIS_HOST} 和端口 {settings.redis.REDIS_PORT} 是否可达"
            ) from exc
        except Exception as exc:
            self._available = False
            await self._close_client_safely()
            raise ConnectionError(f"Redis 连接失败: {exc}") from exc

    async def _async_cleanup(self):
        """关闭 Redis 连接。"""
        await self._close_client_safely()

    async def _close_client_safely(self):
        """安全关闭 Redis 客户端，忽略关闭过程中的异常。"""
        if self.client is not None:
            try:
                # redis-py >= 5.0.1 推荐使用 aclose()，close() 已弃用
                close_method = getattr(self.client, "aclose", None)
                if close_method is not None:
                    await close_method()
                else:
                    await self.client.close()
            except Exception:
                pass
            finally:
                self.client = None

    @property
    def available(self) -> bool:
        """缓存服务当前是否可用。"""
        return self._available and self.client is not None

    async def _execute(self, coro):
        """统一执行 Redis 命令，提供超时、异常捕获和可用性降级。"""
        if not self.available:
            raise ConnectionError("缓存服务当前不可用")
        return await asyncio.wait_for(coro, timeout=REDIS_OPERATION_TIMEOUT)

    async def get(self, key: str) -> Optional[Any]:
        """读取缓存值；JSON 字符串自动反序列化为对象。

        Redis 不可用时返回 None，不会抛出异常。
        """
        try:
            value = await self._execute(self.client.get(key))
            if value is not None:
                try:
                    return json.loads(value)
                except json.JSONDecodeError:
                    return value
        except Exception as exc:
            logger.warning("读取缓存失败 [key=%s]: %s", key, exc)
            self._available = False
        return None

    async def set(self, key: str, value: Any, expire: Optional[timedelta] = None) -> bool:
        """写入缓存值；dict/list 自动序列化为 JSON。

        Redis 不可用时返回 False，不会抛出异常。

        Args:
            key: 缓存键。
            value: 缓存值。
            expire: 可选过期时间。
        """
        try:
            if isinstance(value, (dict, list)):
                value = json.dumps(value)
            if expire:
                return await self._execute(self.client.set(key, value, ex=expire))
            return await self._execute(self.client.set(key, value))
        except Exception as exc:
            logger.warning("写入缓存失败 [key=%s]: %s", key, exc)
            self._available = False
        return False

    async def delete(self, key: str) -> int:
        """删除指定缓存键；失败时返回 0。"""
        try:
            return await self._execute(self.client.delete(key))
        except Exception as exc:
            logger.warning("删除缓存失败 [key=%s]: %s", key, exc)
            self._available = False
        return 0

    async def exists(self, key: str) -> bool:
        """判断缓存键是否存在；失败时返回 False。"""
        try:
            return await self._execute(self.client.exists(key)) == 1
        except Exception as exc:
            logger.warning("判断缓存存在失败 [key=%s]: %s", key, exc)
            self._available = False
        return False

    async def clear_pattern(self, pattern: str) -> None:
        """按模式批量删除缓存键；失败时静默忽略。"""
        try:
            keys = await self._execute(self.client.keys(pattern))
            if keys:
                await self._execute(self.client.delete(*keys))
        except Exception as exc:
            logger.warning("按模式清除缓存失败 [pattern=%s]: %s", pattern, exc)
            self._available = False

    async def ping(self) -> bool:
        """测试 Redis 连接是否可用；失败时返回 False。"""
        if not self.available:
            return False
        try:
            await self._execute(self.client.ping())
            self._available = True
            return True
        except Exception as exc:
            logger.warning("Redis ping 失败: %s", exc)
            self._available = False
            return False