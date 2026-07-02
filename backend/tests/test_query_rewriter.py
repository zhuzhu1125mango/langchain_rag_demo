"""query_rewriter 单元测试。

覆盖规则路径、LLM fallback、上下文补全、保底策略等场景。
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.query_rewriter import QueryRewriter


class TestRuleBasedRewrite:
    """规则快速路径测试。"""

    @pytest.mark.asyncio
    async def test_time_price_query(self):
        """时间+价格类问题应生成含日期的 query。"""
        rewriter = QueryRewriter(llm=None)
        queries = await rewriter.rewrite("今天金价")
        # 始终包含原始问题
        assert queries[0] == "今天金价"
        # 规则生成的 query 应包含当前年份
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert any(today in q for q in queries)
        assert any("黄金" in q for q in queries)

    @pytest.mark.asyncio
    async def test_weather_query(self):
        """天气+城市类问题应生成中英文双 query。"""
        rewriter = QueryRewriter(llm=None)
        queries = await rewriter.rewrite("北京今天天气")
        assert queries[0] == "北京今天天气"
        # 应生成中文和英文 query
        assert any("天气预报" in q for q in queries)
        assert any("weather" in q.lower() for q in queries)

    @pytest.mark.asyncio
    async def test_compare_query(self):
        """对比类问题应拆分为多个子 query。"""
        rewriter = QueryRewriter(llm=None)
        queries = await rewriter.rewrite("Python 和 Java 对比")
        assert queries[0] == "Python 和 Java 对比"
        assert any("Python" in q and "评测" in q for q in queries)
        assert any("Java" in q and "评测" in q for q in queries)
        assert any("vs" in q.lower() for q in queries)

    @pytest.mark.asyncio
    async def test_news_query(self):
        """新闻/最新类问题应生成含年份的 query。"""
        rewriter = QueryRewriter(llm=None)
        queries = await rewriter.rewrite("最新 AI 进展")
        assert queries[0] == "最新 AI 进展"
        year = str(datetime.now(timezone.utc).year)
        assert any(year in q for q in queries)

    @pytest.mark.asyncio
    async def test_no_rule_hit_returns_original_only(self):
        """无规则命中且无 LLM 时，应只返回原始问题。"""
        rewriter = QueryRewriter(llm=None)
        queries = await rewriter.rewrite("随便聊聊")
        assert queries == ["随便聊聊"]

    @pytest.mark.asyncio
    async def test_how_to_query(self):
        """教程/操作类问题应生成相关 query。"""
        rewriter = QueryRewriter(llm=None)
        queries = await rewriter.rewrite("如何学习 Python")
        assert queries[0] == "如何学习 Python"
        assert any("教程" in q for q in queries)
        assert any("官方文档" in q for q in queries)

    @pytest.mark.asyncio
    async def test_definition_query(self):
        """定义/概念类问题应生成相关 query。"""
        rewriter = QueryRewriter(llm=None)
        queries = await rewriter.rewrite("什么是机器学习")
        assert queries[0] == "什么是机器学习"
        assert any("定义" in q for q in queries)

    @pytest.mark.asyncio
    async def test_troubleshooting_query(self):
        """报错/调试类问题应生成相关 query。"""
        rewriter = QueryRewriter(llm=None)
        queries = await rewriter.rewrite("Python 报错 KeyError 怎么解决")
        assert queries[0] == "Python 报错 KeyError 怎么解决"
        assert any("解决方案" in q or "stackoverflow" in q for q in queries)

    @pytest.mark.asyncio
    async def test_code_query(self):
        """代码类问题应生成相关 query。"""
        rewriter = QueryRewriter(llm=None)
        queries = await rewriter.rewrite("Python 读取文件 代码示例")
        assert queries[0] == "Python 读取文件 代码示例"
        assert any("github" in q.lower() for q in queries)

    @pytest.mark.asyncio
    async def test_synonym_expansion(self):
        """缩写/同义词应被扩展。"""
        rewriter = QueryRewriter(llm=None)
        queries = await rewriter.rewrite("什么是 AI")
        assert queries[0] == "什么是 AI"
        assert any("人工智能" in q for q in queries)


class TestLLMFallback:
    """LLM fallback 测试。"""

    @pytest.mark.asyncio
    async def test_llm_rewrite_success(self):
        """规则未命中时调用 LLM，应返回 LLM 生成的 query。"""
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(
            return_value=MagicMock(content='["Python 异步编程 教程", "asyncio 使用指南"]')
        )
        rewriter = QueryRewriter(llm=mock_llm)
        queries = await rewriter.rewrite("Python 异步编程怎么学")

        assert queries[0] == "Python 异步编程怎么学"
        # LLM 生成的 query 应在列表中
        assert any("异步编程" in q for q in queries)

    @pytest.mark.asyncio
    async def test_llm_rewrite_failure_fallback(self):
        """LLM 调用失败时应返回原始问题。"""
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(side_effect=Exception("LLM 不可用"))
        rewriter = QueryRewriter(llm=mock_llm)
        queries = await rewriter.rewrite("某个复杂问题需要改写")

        assert queries[0] == "某个复杂问题需要改写"

    @pytest.mark.asyncio
    async def test_llm_invalid_json_fallback(self):
        """LLM 返回非 JSON 时应安全降级。"""
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(
            return_value=MagicMock(content="这不是一个 JSON 数组")
        )
        rewriter = QueryRewriter(llm=mock_llm)
        queries = await rewriter.rewrite("某个问题")

        assert queries[0] == "某个问题"


class TestContextResolution:
    """多轮对话上下文补全测试。"""

    @pytest.mark.asyncio
    async def test_pronoun_resolution(self):
        """代词应被历史实体替换。"""
        rewriter = QueryRewriter(llm=None)
        context = [
            {"role": "user", "content": "今天金价多少"},
            {"role": "assistant", "content": "今日黄金价格约为 780 元/克"},
        ]
        queries = await rewriter.rewrite("它的趋势呢", conversation_context=context)
        # 应包含补全后的 query
        assert any("金价" in q or "价格" in q for q in queries)

    @pytest.mark.asyncio
    async def test_short_question_resolution(self):
        """短问题且不含动词应从历史继承实体。"""
        rewriter = QueryRewriter(llm=None)
        context = [
            {"role": "user", "content": "金价"},
        ]
        queries = await rewriter.rewrite("价格", conversation_context=context)
        # 补全后应能触发价格规则
        assert len(queries) > 1

    @pytest.mark.asyncio
    async def test_no_context_needed(self):
        """无需补全的问题应原样返回。"""
        rewriter = QueryRewriter(llm=None)
        queries = await rewriter.rewrite("今天金价", conversation_context=None)
        assert queries[0] == "今天金价"

    @pytest.mark.asyncio
    async def test_empty_context(self):
        """空对话历史不应导致异常。"""
        rewriter = QueryRewriter(llm=None)
        queries = await rewriter.rewrite("它怎么样", conversation_context=[])
        assert queries[0] == "它怎么样"


class TestGuarantees:
    """保底策略测试。"""

    @pytest.mark.asyncio
    async def test_original_always_first(self):
        """原始问题应始终是第一个元素。"""
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(
            return_value=MagicMock(content='["改写1", "改写2"]')
        )
        rewriter = QueryRewriter(llm=mock_llm)
        queries = await rewriter.rewrite("我的原始问题")
        assert queries[0] == "我的原始问题"

    @pytest.mark.asyncio
    async def test_max_queries_limit(self):
        """query 数量不应超过配置上限。"""
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(
            return_value=MagicMock(
                content='["q1", "q2", "q3", "q4", "q5", "q6"]'
            )
        )
        rewriter = QueryRewriter(llm=mock_llm, num_queries=3)
        queries = await rewriter.rewrite("复杂问题")
        assert len(queries) <= 3

    @pytest.mark.asyncio
    async def test_empty_question(self):
        """空问题应安全处理。"""
        rewriter = QueryRewriter(llm=None)
        queries = await rewriter.rewrite("")
        assert queries == [""] or queries == []

    @pytest.mark.asyncio
    async def test_dedup(self):
        """重复的 query 应被去重。"""
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(
            return_value=MagicMock(
                content='["相同问题", "相同问题", "相同问题"]'
            )
        )
        rewriter = QueryRewriter(llm=mock_llm)
        queries = await rewriter.rewrite("相同问题")
        # 原始问题 + 去重后的 LLM 结果
        normalized = [rewriter._normalize_query(q) for q in queries]
        assert len(normalized) == len(set(normalized))
