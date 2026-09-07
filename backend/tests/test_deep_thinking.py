"""深度思考开关单元测试。

覆盖 should_think 开关决策、MessageRequest 校验、
AnswerGenerator 的 think 参数绑定行为。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from src.api.chat import MessageRequest
from src.config import settings
from src.services.answer_generator import AnswerGenerator
from src.services.rag_chain import should_think


class TestShouldThink:
    """should_think 开关决策测试。"""

    def test_on_returns_true(self):
        assert should_think("on") is True

    def test_off_returns_false(self):
        assert should_think("off") is False

    def test_default_off(self):
        assert should_think() is False

    def test_model_unsupported_returns_none(self, monkeypatch):
        """模型不支持思考 → 不干预（None），即使显式 on/off。"""
        monkeypatch.setattr(settings.model, "OLLAMA_SUPPORTS_THINKING", False)
        assert should_think("on") is None
        assert should_think("off") is None


class TestMessageRequestDeepThinking:
    """MessageRequest.deep_thinking 校验测试。"""

    def test_default_off(self):
        req = MessageRequest(question="你好")
        assert req.deep_thinking == "off"

    def test_valid_values(self):
        for v in ("on", "off"):
            assert MessageRequest(question="q", deep_thinking=v).deep_thinking == v

    def test_invalid_value_rejected(self):
        for v in ("auto", "always"):
            with pytest.raises(ValidationError):
                MessageRequest(question="q", deep_thinking=v)

    def test_none_normalized_to_off(self):
        assert MessageRequest(question="q", deep_thinking=None).deep_thinking == "off"


class TestAnswerGeneratorThink:
    """AnswerGenerator think 参数绑定行为测试。"""

    @staticmethod
    def _make_llm(content: str = "答案"):
        """构造 Mock LLM；bind 返回独立的绑定对象以便断言。"""
        llm = MagicMock()
        resp = MagicMock()
        resp.content = content
        llm.ainvoke = AsyncMock(return_value=resp)
        bound = MagicMock()
        bound.ainvoke = AsyncMock(return_value=resp)
        llm.bind = MagicMock(return_value=bound)
        return llm, bound

    @pytest.mark.asyncio
    async def test_think_false_binds_llm(self):
        """think=False → 应通过 bind 传递且不再直接调用原 llm。"""
        llm, bound = self._make_llm()
        generator = AnswerGenerator(llm=llm)

        await generator.generate(question="你好", think=False)

        llm.bind.assert_called_once_with(reasoning=False)
        bound.ainvoke.assert_awaited_once()
        llm.ainvoke.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_think_none_skips_bind(self):
        """think=None → 不 bind，保持模型默认行为。"""
        llm, bound = self._make_llm()
        generator = AnswerGenerator(llm=llm)

        await generator.generate(question="你好")

        llm.bind.assert_not_called()
        llm.ainvoke.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_generate_stream_think_false_binds(self):
        """流式路径 think=False → 同样 bind 且 astream 用于绑定对象。"""
        llm, bound = self._make_llm()

        async def _empty_stream(_prompt):
            chunk = MagicMock()
            chunk.content = "x"
            chunk.additional_kwargs = {}
            yield chunk

        llm.astream = MagicMock(return_value=_empty_stream(None))
        bound.astream = MagicMock(return_value=_empty_stream(None))
        generator = AnswerGenerator(llm=llm)

        chunks = [c async for c, _, _ in generator.generate_stream(question="你好", think=False)]

        llm.bind.assert_called_once_with(reasoning=False)
        bound.astream.assert_called_once()
        llm.astream.assert_not_called()
        assert chunks == ["x"]

    @pytest.mark.asyncio
    async def test_generate_stream_forwards_thinking(self):
        """流式路径转发思考增量：additional_kwargs['reasoning_content'] 经第三元组返回。"""
        llm, bound = self._make_llm()

        async def _think_stream(_prompt):
            thinking = MagicMock()
            thinking.content = ""
            thinking.additional_kwargs = {"reasoning_content": "思考中"}
            content = MagicMock()
            content.content = "答案"
            content.additional_kwargs = {}
            yield thinking
            yield content

        llm.astream = MagicMock(return_value=_think_stream(None))
        bound.astream = MagicMock(return_value=_think_stream(None))
        generator = AnswerGenerator(llm=llm)

        items = [item async for item in generator.generate_stream(question="你好", think=True)]

        llm.bind.assert_called_once_with(reasoning=True)
        assert [i[0] for i in items] == ["", "答案"]
        assert [i[2] for i in items] == ["思考中", ""]

    @pytest.mark.asyncio
    async def test_generate_with_citation_passes_think(self):
        """generate_with_citation 透传 think 到 generate。"""
        llm, bound = self._make_llm("今日金价 850 元。")
        generator = AnswerGenerator(llm=llm)

        await generator.generate_with_citation(question="今日金价", think=False)

        llm.bind.assert_called_once_with(reasoning=False)
        bound.ainvoke.assert_awaited_once()
