"""MilvusService 单元测试。"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from pymilvus.exceptions import MilvusException

from src.services.milvus_service import MilvusService


@pytest.fixture
def service():
    """构造一个未初始化的 MilvusService 实例用于测试私有方法。"""
    svc = MilvusService.__new__(MilvusService)
    limiter = MagicMock()
    limiter.__aenter__ = AsyncMock(return_value=None)
    limiter.__aexit__ = AsyncMock(return_value=None)
    svc._flush_limiter = limiter
    svc.client = MagicMock()
    svc.client.flush = AsyncMock()
    return svc


class TestLimiterConfig:
    """测试 flush 限流器配置转换。"""

    def test_limiter_uses_single_capacity(self, monkeypatch):
        """QPS 配置应转换为 max_rate=1, time_period=1/QPS，避免容量不足报错。"""
        monkeypatch.setattr(
            "src.services.milvus_service.settings.milvus.MILVUS_FLUSH_RATE_LIMIT", 0.15
        )
        svc = MilvusService.__new__(MilvusService)
        svc.__init__()
        assert svc._flush_limiter.max_rate == 1
        assert svc._flush_limiter.time_period == pytest.approx(1 / 0.15, abs=0.01)

    @pytest.mark.asyncio
    async def test_limiter_value_error_fallback(self, service):
        """限流器配置异常时，应记录错误并继续执行 flush。"""
        service._flush_limiter.__aenter__.side_effect = ValueError("capacity exceeded")
        await service._throttled_flush()
        service.client.flush.assert_awaited_once()


class TestThrottledFlush:
    """测试受限速保护的 flush 方法。"""

    @pytest.mark.asyncio
    async def test_flush_succeeds_on_first_attempt(self, service):
        """首次 flush 成功时直接返回。"""
        await service._throttled_flush()

        service._flush_limiter.__aenter__.assert_awaited_once()
        service.client.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rate_limit_retry_then_success(self, service, monkeypatch):
        """触发 rate limit 后指数退避并最终成功。"""
        monkeypatch.setattr(
            "src.services.milvus_service.settings.milvus.MILVUS_FLUSH_MAX_RETRY", 3
        )
        monkeypatch.setattr(
            "src.services.milvus_service.settings.milvus.MILVUS_FLUSH_BASE_WAIT", 0.01
        )

        exc = MilvusException(message="rate limit exceeded[rate=0.1]")
        service.client.flush.side_effect = [exc, exc, None]

        await service._throttled_flush()

        assert service.client.flush.await_count == 3

    @pytest.mark.asyncio
    async def test_rate_limit_exceeds_max_retry_raises(self, service, monkeypatch):
        """rate limit 超过最大重试次数后抛出异常。"""
        monkeypatch.setattr(
            "src.services.milvus_service.settings.milvus.MILVUS_FLUSH_MAX_RETRY", 2
        )
        monkeypatch.setattr(
            "src.services.milvus_service.settings.milvus.MILVUS_FLUSH_BASE_WAIT", 0.01
        )

        exc = MilvusException(message="rate limit exceeded[rate=0.1]")
        service.client.flush.side_effect = [exc, exc, exc]

        with pytest.raises(MilvusException):
            await service._throttled_flush()

        assert service.client.flush.await_count == 3

    @pytest.mark.asyncio
    async def test_non_rate_limit_error_does_not_retry(self, service, monkeypatch):
        """非 rate limit 异常不重试，直接抛出。"""
        monkeypatch.setattr(
            "src.services.milvus_service.settings.milvus.MILVUS_FLUSH_MAX_RETRY", 3
        )

        exc = MilvusException(message="some other error")
        service.client.flush.side_effect = exc

        with pytest.raises(MilvusException):
            await service._throttled_flush()

        assert service.client.flush.await_count == 1


class TestIsRateLimitError:
    """测试 rate limit 错误识别。"""

    def test_code_eight_recognized(self):
        exc = MilvusException(message="rate limit", code=8)
        assert MilvusService._is_rate_limit_error(exc) is True

    def test_message_keyword_recognized(self):
        exc = MilvusException(message="request is rejected by grpc RateLimiter middleware")
        assert MilvusService._is_rate_limit_error(exc) is True

    def test_other_error_not_recognized(self):
        exc = MilvusException(message="collection not found", code=1)
        assert MilvusService._is_rate_limit_error(exc) is False
