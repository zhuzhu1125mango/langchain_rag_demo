"""CacheService 单元测试。"""

import pytest

from src.services.cache_service import CacheService


@pytest.mark.asyncio
async def test_async_init_raises_when_password_missing(monkeypatch):
    """REDIS_PASSWORD 未配置时应抛出明确错误。"""
    monkeypatch.setattr("src.services.cache_service.settings.redis.REDIS_PASSWORD", "")
    cache = CacheService.__new__(CacheService)
    with pytest.raises(ValueError, match="REDIS_PASSWORD"):
        await cache._async_init()
