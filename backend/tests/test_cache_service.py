"""CacheService 单元测试。"""

import pytest

from src.services.cache_service import CacheService


class _FakeRedis:
    """拦截真实连接的最小 Redis 桩（仅覆盖 _async_init 用到的接口）。"""

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def ping(self):
        return True

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_async_init_empty_password_dev_allowed(monkeypatch):
    """W6 #10：开发模式空密码允许无密码连接（不再抛 ValueError）。"""
    monkeypatch.setattr("src.services.cache_service.settings.redis.REDIS_PASSWORD", "")
    monkeypatch.setattr("src.services.cache_service.aioredis.Redis", _FakeRedis)
    cache = CacheService()
    await cache._async_init()
    assert cache._available is True
    assert cache.client.kwargs["password"] is None, "空密码应传 None（等同无密码）"


@pytest.mark.asyncio
async def test_async_init_raises_when_password_missing_in_production(monkeypatch):
    """W6 #10：生产模式空密码必须拒绝启动（防止裸奔 Redis 上线）。"""
    monkeypatch.setattr("src.services.cache_service.settings.redis.REDIS_PASSWORD", "")
    monkeypatch.setattr("src.services.cache_service.settings.APP_ENV", "prod")
    cache = CacheService()
    with pytest.raises(ValueError, match="REDIS_PASSWORD"):
        await cache._async_init()
