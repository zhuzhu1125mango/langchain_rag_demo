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

    def test_non_reasoning_model_forces_off(self):
        """模型不具备思考能力时，请求 on 也只能是 False。

        这一条不是「顺手过滤」：开关对一个不会思考的模型打开，用户得到的是
        同一个更慢的回答，还会以为自己在用深度思考。
        """
        assert should_think("on", thinking_control="off") is False

    def test_non_reasoning_model_forces_off_even_when_global_disabled(self, monkeypatch):
        """全局开关也没开时，仍然要给出明确的 False，而不是 None。"""
        monkeypatch.setattr(settings.model, "OLLAMA_SUPPORTS_THINKING", False)
        assert should_think("on", thinking_control="off") is False

    def test_switchable_model_follows_switch(self):
        assert should_think("on", thinking_control="switchable") is True
        assert should_think("off", thinking_control="switchable") is False

    def test_always_on_model_is_not_pretended_to_be_off(self):
        """会思考但关不掉 → None（不发参数），而不是 False。

        传 False 只会制造「我们关掉了」的假象：LM Studio 上的 qwen3 实测发
        `reasoning_effort` / `chat_template_kwargs.enable_thinking` /
        `enable_thinking` / `think` 都照样思考（reasoning_tokens 290→274）。
        既然关不掉，就别假装能关——日志里也该看得出这一点。
        """
        assert should_think("on", thinking_control="always_on") is None
        assert should_think("off", thinking_control="always_on") is None

    def test_unknown_control_keeps_global_behavior(self):
        """可控性未知（没选模型 / 注册表查不到）时不替用户改主意。"""
        assert should_think("on", thinking_control=None) is True
        assert should_think("off", thinking_control=None) is False


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
    """AnswerGenerator 按 think 选择 registry 预配置模型，不再业务层 bind。"""

    @staticmethod
    def _make_llm(content: str = "答案"):
        llm = MagicMock()
        resp = MagicMock()
        resp.content = content
        llm.ainvoke = AsyncMock(return_value=resp)
        llm.bind = MagicMock(side_effect=AssertionError("业务层不应再 bind(reasoning=...)"))
        return llm

    @pytest.mark.asyncio
    async def test_think_false_uses_direct_model(self):
        """think=False → 使用 answer_direct 路由模型。"""
        llm = self._make_llm()
        direct = self._make_llm("直答")
        generator = AnswerGenerator(llm=llm, llm_direct=direct)

        await generator.generate(question="你好", think=False)

        direct.ainvoke.assert_awaited_once()
        llm.ainvoke.assert_not_awaited()
        llm.bind.assert_not_called()
        direct.bind.assert_not_called()

    @pytest.mark.asyncio
    async def test_think_false_without_direct_falls_back_main(self):
        """未配置 direct route 时回退主模型，仍不 bind。"""
        llm = self._make_llm()
        generator = AnswerGenerator(llm=llm)

        await generator.generate(question="你好", think=False)

        llm.ainvoke.assert_awaited_once()
        llm.bind.assert_not_called()

    @pytest.mark.asyncio
    async def test_think_none_and_true_use_main_model(self):
        """think=None/True → 主回答模型，保持模型默认行为。"""
        for think in (None, True):
            llm = self._make_llm()
            generator = AnswerGenerator(llm=llm)

            await generator.generate(question="你好", think=think)

            llm.ainvoke.assert_awaited_once()
            llm.bind.assert_not_called()

    @pytest.mark.asyncio
    async def test_generate_stream_think_false_uses_direct(self):
        """流式路径 think=False → 使用 direct 模型。"""
        llm = self._make_llm()
        direct = self._make_llm()

        async def _empty_stream(_prompt):
            chunk = MagicMock()
            chunk.content = "x"
            chunk.additional_kwargs = {}
            yield chunk

        llm.astream = MagicMock(return_value=_empty_stream(None))
        direct.astream = MagicMock(return_value=_empty_stream(None))
        generator = AnswerGenerator(llm=llm, llm_direct=direct)

        chunks = [c async for c, _, _ in generator.generate_stream(question="你好", think=False)]

        direct.astream.assert_called_once()
        llm.astream.assert_not_called()
        assert chunks == ["x"]

    @pytest.mark.asyncio
    async def test_generate_stream_forwards_thinking(self):
        """流式路径转发思考增量：additional_kwargs['reasoning_content'] 经第三元组返回。"""
        llm = self._make_llm()

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
        generator = AnswerGenerator(llm=llm)

        items = [item async for item in generator.generate_stream(question="你好", think=True)]

        llm.bind.assert_not_called()
        assert [i[0] for i in items] == ["", "答案"]
        assert [i[2] for i in items] == ["思考中", ""]

    @pytest.mark.asyncio
    async def test_generate_with_citation_passes_think(self):
        """generate_with_citation 透传 think 到 generate。"""
        llm = self._make_llm()
        direct = self._make_llm("今日金价 850 元。")
        generator = AnswerGenerator(llm=llm, llm_direct=direct)

        await generator.generate_with_citation(question="今日金价", think=False)

        direct.ainvoke.assert_awaited_once()
        llm.ainvoke.assert_not_awaited()
