"""answer_generator generate_with_citation 单元测试。

覆盖引用补全集成、降级策略、异常容错等场景。
使用 Mock LLM 和 Mock CitationBackfiller 避免外部依赖。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.answer_generator import AnswerGenerator


def _make_llm(content: str = "测试答案。"):
    """构造 Mock LLM，ainvoke 返回指定内容。"""
    llm = MagicMock()
    resp = MagicMock()
    resp.content = content
    llm.ainvoke = AsyncMock(return_value=resp)
    return llm


def _make_backfiller(result: str = "补全后答案[1]。", raise_error: bool = False):
    """构造 Mock CitationBackfiller。"""
    backfiller = MagicMock()
    if raise_error:
        backfiller.backfill = AsyncMock(side_effect=RuntimeError("backfill failed"))
    else:
        backfiller.backfill = AsyncMock(return_value=result)
    return backfiller


class TestGenerateWithCitation:
    """generate_with_citation 集成测试。"""

    @pytest.mark.asyncio
    async def test_normal_flow_with_backfiller(self):
        """有 backfiller 和 search_sources → 应调用 backfill 补全。"""
        llm = _make_llm("今日金价 850 元。")
        generator = AnswerGenerator(llm=llm)
        backfiller = _make_backfiller(result="今日金价 850 元[1]。")

        answer, sources = await generator.generate_with_citation(
            question="今日金价",
            search_sources=[{"source_index": 1, "title": "金价", "content": "850 元"}],
            citation_backfiller=backfiller,
        )

        assert answer == "今日金价 850 元[1]。"
        backfiller.backfill.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_backfiller_returns_raw(self):
        """无 backfiller → 返回原始答案，不补全。"""
        llm = _make_llm("今日金价 850 元。")
        generator = AnswerGenerator(llm=llm)

        answer, sources = await generator.generate_with_citation(
            question="今日金价",
            search_sources=[{"source_index": 1, "title": "金价", "content": "850 元"}],
            citation_backfiller=None,
        )

        assert answer == "今日金价 850 元。"

    @pytest.mark.asyncio
    async def test_no_search_sources_returns_raw(self):
        """无 search_sources → 返回原始答案，不补全。"""
        llm = _make_llm("今日金价 850 元。")
        generator = AnswerGenerator(llm=llm)
        backfiller = _make_backfiller(result="不应被使用[1]。")

        answer, sources = await generator.generate_with_citation(
            question="今日金价",
            search_sources=None,
            citation_backfiller=backfiller,
        )

        assert answer == "今日金价 850 元。"
        backfiller.backfill.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_search_sources_returns_raw(self):
        """空 search_sources 列表 → 返回原始答案，不补全。"""
        llm = _make_llm("今日金价 850 元。")
        generator = AnswerGenerator(llm=llm)
        backfiller = _make_backfiller(result="不应被使用[1]。")

        answer, sources = await generator.generate_with_citation(
            question="今日金价",
            search_sources=[],
            citation_backfiller=backfiller,
        )

        assert answer == "今日金价 850 元。"
        backfiller.backfill.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_backfill_failure_returns_raw(self):
        """backfill 抛异常 → 保留原始答案，不阻塞主流程。"""
        llm = _make_llm("今日金价 850 元。")
        generator = AnswerGenerator(llm=llm)
        backfiller = _make_backfiller(raise_error=True)

        answer, sources = await generator.generate_with_citation(
            question="今日金价",
            search_sources=[{"source_index": 1, "title": "金价", "content": "850 元"}],
            citation_backfiller=backfiller,
        )

        # 异常被捕获，返回原始答案
        assert answer == "今日金价 850 元。"

    @pytest.mark.asyncio
    async def test_sources_passed_through(self):
        """应透传 generate 返回的 sources 元数据。"""
        llm = _make_llm("答案。")
        generator = AnswerGenerator(llm=llm)
        backfiller = _make_backfiller(result="答案[1]。")

        answer, sources = await generator.generate_with_citation(
            question="问题",
            search_sources=[{"source_index": 1, "title": "t", "content": "c"}],
            citation_backfiller=backfiller,
        )

        # sources 透传（无工具/知识库时为空列表）
        assert isinstance(sources, list)


class TestPromptChange:
    """Prompt 改造验证测试。"""

    async def test_inline_prompt_uses_encourage(self):
        """内联 fallback prompt 应使用"尽量"而非"必须"。"""
        llm = _make_llm()
        generator = AnswerGenerator(llm=llm)
        prompt = await generator._build_prompt(
            question="问题",
            history_context="",
            context="参考信息",
        )
        assert "尽量在事实性陈述后标注来源编号" in prompt
        assert "系统会自动补全" in prompt
        # 不应再出现"必须标注来源编号"
        assert "必须标注来源编号" not in prompt

    async def test_no_context_prompt_no_must(self):
        """无上下文 prompt 不应包含"必须标注来源编号"。"""
        llm = _make_llm()
        generator = AnswerGenerator(llm=llm)
        prompt = await generator._build_prompt(
            question="问题",
            history_context="",
            context="",
            no_context=True,
        )
        assert "必须标注来源编号" not in prompt
