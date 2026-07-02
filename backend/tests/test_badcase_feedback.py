"""Badcase 反馈处理器单元测试。"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.services.badcase import BadcaseCategory, BadcaseFeedbackHandler
from src.services.badcase.feedback_handler import BadcaseCreateRequest


class TestBadcaseClassification:
    """Badcase 自动分类测试。"""

    def test_retrieval_failure_no_sources(self):
        """无检索来源时应判定为检索失败。"""
        result = BadcaseFeedbackHandler.classify(
            question="q", answer="a", retrieved_sources=None, reason=""
        )
        assert result["category"] == BadcaseCategory.RETRIEVAL_FAILURE.value
        assert result["severity"] == 4

    def test_retrieval_failure_keyword(self):
        """包含检索失败关键词时应判定为检索失败。"""
        result = BadcaseFeedbackHandler.classify(
            question="q",
            answer="a",
            retrieved_sources=[{"content": "x"}],
            reason="找不到相关内容",
        )
        assert result["category"] == BadcaseCategory.RETRIEVAL_FAILURE.value

    def test_hallucination(self):
        """幻觉关键词应判定为幻觉。"""
        result = BadcaseFeedbackHandler.classify(
            question="q",
            answer="a",
            retrieved_sources=[{"content": "x"}],
            reason="回答编造了不存在的信息",
        )
        assert result["category"] == BadcaseCategory.HALLUCINATION.value
        assert result["severity"] == 5

    def test_incomplete_answer(self):
        """不完整关键词应判定为回答不完整。"""
        result = BadcaseFeedbackHandler.classify(
            question="q",
            answer="a",
            retrieved_sources=[{"content": "x"}],
            reason="回答不够详细，漏了关键信息",
        )
        assert result["category"] == BadcaseCategory.INCOMPLETE_ANSWER.value

    def test_wrong_answer(self):
        """错误关键词应判定为错误答案。"""
        result = BadcaseFeedbackHandler.classify(
            question="q",
            answer="a",
            retrieved_sources=[{"content": "x"}],
            reason="数据不准确",
        )
        assert result["category"] == BadcaseCategory.WRONG_ANSWER.value

    def test_negative_feedback_short_answer(self):
        """差评且答案过短应判定为不完整。"""
        result = BadcaseFeedbackHandler.classify(
            question="q",
            answer="short",
            retrieved_sources=[{"content": "x"}],
            feedback_type="dislike",
        )
        assert result["category"] == BadcaseCategory.INCOMPLETE_ANSWER.value


class TestBadcaseHandler:
    """Badcase 处理器流程测试。"""

    @pytest.mark.asyncio
    async def test_handle_without_db(self, monkeypatch):
        """无数据库会话时应完成分类与学习并返回结果。"""
        handler = BadcaseFeedbackHandler()
        # Mock 在线学习避免初始化 embedding 模型
        monkeypatch.setattr(
            handler,
            "_trigger_learning",
            AsyncMock(return_value={"status": "correct"}),
        )

        request = BadcaseCreateRequest(
            question="测试问题",
            answer="测试答案",
            reason="回答编造了不存在的信息",
            retrieved_sources=[{"content": "source"}],
        )
        result = await handler.handle(request)
        assert result["category"] == BadcaseCategory.HALLUCINATION.value
        assert result["severity"] == 5
        assert result["learning_result"]["status"] == "correct"

    @pytest.mark.asyncio
    async def test_handle_with_mock_db(self, monkeypatch):
        """带数据库会话时应尝试保存记录并返回 badcase_id。"""
        handler = BadcaseFeedbackHandler()
        monkeypatch.setattr(
            handler,
            "_trigger_learning",
            AsyncMock(return_value={"status": "misclassification_learned"}),
        )

        mock_session = MagicMock()
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        mock_session.refresh = AsyncMock()

        request = BadcaseCreateRequest(
            question="测试问题",
            answer="测试答案",
            reason="找不到相关内容",
        )
        result = await handler.handle(request, db_session=mock_session)
        assert result["category"] == BadcaseCategory.RETRIEVAL_FAILURE.value
        assert result["learning_result"]["status"] == "misclassification_learned"
        # 验证数据库操作被调用
        mock_session.add.assert_called_once()
        mock_session.commit.assert_awaited_once()
        mock_session.refresh.assert_awaited_once()
