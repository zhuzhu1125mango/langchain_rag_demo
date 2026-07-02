"""Redis 缓存服务。

为搜索结果、网页正文等提供统一的缓存读写接口，自动处理 JSON 序列化。
"""

import asyncio
import json
from datetime import timedelta
from typing import Optional, Any

import redis.asyncio as aioredis

from src.config import settings


class CacheService:
    """Redis 客户端封装，支持单例访问与常用缓存操作。"""

    _instance = None
    _lock = asyncio.Lock()
    _initialized = False

    def __init__(self):
        self.client = None

    async def _init_client(self):
        """初始化异步 Redis 连接。"""
        password = settings.redis.REDIS_PASSWORD
        if not password:
            raise ValueError(
                "REDIS_PASSWORD 未配置。Redis 密码为必填项，请在 .env 中设置 REDIS_PASSWORD。"
            )
        self.client = aioredis.Redis(
            host=settings.redis.REDIS_HOST,
            port=settings.redis.REDIS_PORT,
            db=settings.redis.REDIS_DB,
            password=password,
            decode_responses=True,
            protocol=2
        )

    @classmethod
    async def get_instance(cls) -> "CacheService":
        """获取 CacheService 单例。"""
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
                    await cls._instance._init_client()
                    cls._initialized = True
        return cls._instance

    async def get(self, key: str) -> Optional[Any]:
        """读取缓存值；JSON 字符串自动反序列化为对象。"""
        value = await self.client.get(key)
        if value is not None:
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return None

    async def set(self, key: str, value: Any, expire: Optional[timedelta] = None) -> bool:
        """写入缓存值；dict/list 自动序列化为 JSON。

        Args:
            key: 缓存键。
            value: 缓存值。
            expire: 可选过期时间。
        """
        if isinstance(value, (dict, list)):
            value = json.dumps(value)
        if expire:
            return await self.client.set(key, value, ex=expire)
        return await self.client.set(key, value)

    async def delete(self, key: str) -> int:
        """删除指定缓存键。"""
        return await self.client.delete(key)

    async def exists(self, key: str) -> bool:
        """判断缓存键是否存在。"""
        return await self.client.exists(key) == 1

    async def clear_pattern(self, pattern: str) -> None:
        """按模式批量删除缓存键。"""
        keys = await self.client.keys(pattern)
        if keys:
            await self.client.delete(*keys)

    async def ping(self) -> bool:
        """测试 Redis 连接是否可用。"""
        return await self.client.ping()
