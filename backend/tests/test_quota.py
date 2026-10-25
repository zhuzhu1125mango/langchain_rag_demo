"""SEC-2 聊天/上传配额单测（进程内降级路径）。"""

import pytest
from fastapi import HTTPException

from src.config import settings
from src.utils import quota
from src.utils.rate_limit import reset_rate_limits


@pytest.fixture(autouse=True)
def _quota_env(monkeypatch):
    saved = {
        "enabled": settings.security.QUOTA_ENFORCEMENT_ENABLED,
        "rpm": settings.security.CHAT_RATE_LIMIT_PER_MINUTE,
        "concurrent": settings.security.CHAT_MAX_CONCURRENT_STREAMS_PER_USER,
        "daily": settings.security.CHAT_DAILY_MESSAGE_QUOTA,
        "upload_daily": settings.security.UPLOAD_DAILY_MB_PER_USER,
        "app_env": settings.APP_ENV,
        "in_docker": settings.IN_DOCKER,
    }
    settings.security.QUOTA_ENFORCEMENT_ENABLED = True
    settings.security.CHAT_RATE_LIMIT_PER_MINUTE = 100
    settings.security.CHAT_MAX_CONCURRENT_STREAMS_PER_USER = 10
    settings.security.CHAT_DAILY_MESSAGE_QUOTA = 100
    settings.security.UPLOAD_DAILY_MB_PER_USER = 1
    settings.APP_ENV = "dev"       # 进程内降级路径
    settings.IN_DOCKER = False
    quota.reset_memory_quotas()
    reset_rate_limits()
    monkeypatch.setattr(quota, "get_redis_client_if_initialized", lambda: None)
    yield
    settings.security.QUOTA_ENFORCEMENT_ENABLED = saved["enabled"]
    settings.security.CHAT_RATE_LIMIT_PER_MINUTE = saved["rpm"]
    settings.security.CHAT_MAX_CONCURRENT_STREAMS_PER_USER = saved["concurrent"]
    settings.security.CHAT_DAILY_MESSAGE_QUOTA = saved["daily"]
    settings.security.UPLOAD_DAILY_MB_PER_USER = saved["upload_daily"]
    settings.APP_ENV = saved["app_env"]
    settings.IN_DOCKER = saved["in_docker"]
    quota.reset_memory_quotas()
    reset_rate_limits()


@pytest.mark.asyncio
async def test_upload_daily_quota_and_refund():
    limit = 1024 * 1024
    assert await quota.consume_upload_quota("u1", limit // 2) is True
    assert await quota.consume_upload_quota("u1", limit // 2) is True
    assert await quota.consume_upload_quota("u1", 1) is False
    await quota.refund_upload_quota("u1", limit // 2)
    assert await quota.consume_upload_quota("u1", limit // 2) is True


@pytest.mark.asyncio
async def test_chat_concurrency_slot_released():
    settings.security.CHAT_MAX_CONCURRENT_STREAMS_PER_USER = 1
    await quota.acquire_chat_slot("u2")
    with pytest.raises(HTTPException) as exc:
        await quota.acquire_chat_slot("u2")
    assert exc.value.status_code == 429
    await quota.release_chat_slot("u2")
    await quota.acquire_chat_slot("u2")
    await quota.release_chat_slot("u2")


@pytest.mark.asyncio
async def test_chat_daily_quota_blocks_after_release():
    settings.security.CHAT_DAILY_MESSAGE_QUOTA = 1
    await quota.acquire_chat_slot("u3")
    await quota.release_chat_slot("u3")
    with pytest.raises(HTTPException) as exc:
        await quota.acquire_chat_slot("u3")
    assert exc.value.status_code == 429
    await quota.release_chat_slot("u3")


@pytest.mark.asyncio
async def test_quota_disabled_is_noop():
    settings.security.QUOTA_ENFORCEMENT_ENABLED = False
    await quota.acquire_chat_slot("u4")
    await quota.release_chat_slot("u4")
    assert await quota.consume_upload_quota("u4", 10 * 1024 * 1024) is True
