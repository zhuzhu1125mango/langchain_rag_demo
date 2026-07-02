"""
RAG 系统 evaluation 测试集。

覆盖阶段三核心能力：
1. 输出清洗（OutputSanitizer）
2. 意图路由（IntentRouter）
3. 答案生成（AnswerGenerator）
4. 链路追踪（TraceCollector）
5. 搜索后处理（SearchPostprocessor）
6. 工具插件（weather / datetime / calculator）
7. 端到端场景（weather / time / kb / web search / agent）

端到端测试默认跳过，需在本地启动 Ollama / Milvus / SearXNG / PostgreSQL 后手动运行。
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.services.answer_generator import AnswerGenerator
from src.services.intent_router import FallbackStrategy, IntentRouter, PrimaryMode
from src.services.output_sanitizer import OutputSanitizer
from src.services.search_postprocessor import SearchPostprocessor
from src.services.tools.plugins.calculator_tool import CalculatorTool
from src.services.tools.plugins.datetime_tool import DateTimeTool, build_datetime_answer
from src.services.tools.plugins.weather_tool import WeatherTool
from src.services.tools.weather_tool import _extract_city_name
from src.services.tools.tool_manager import ToolManager, ToolResult
from src.services.search_types import SearchResult
from src.services.trace_collector import TraceCollector


# =============================================================================
# 1. 输出清洗
# =============================================================================
class TestOutputSanitizer:
    """输出清洗测试。"""

    def test_detect_tool_call_json(self):
        """应识别 LLM 输出的工具调用 JSON 污染。"""
        polluted = '[{"name": "web_search", "arguments": {"query": "吕梁天气"}}]'
        assert OutputSanitizer.looks_like_tool_call(polluted) is True

    def test_detect_tool_call_single(self):
        """应识别单条工具调用 JSON。"""
        polluted = '{"name": "weather_query", "arguments": {"city": "北京"}}'
        assert OutputSanitizer.looks_like_tool_call(polluted) is True

    def test_no_pollution(self):
        """正常自然语言不应被误判。"""
        text = "北京今天天气晴朗，气温约 25°C。"
        assert OutputSanitizer.looks_like_tool_call(text) is False

    def test_remove_thinking_tags(self):
        """应移除 <think> 思考标签。"""
        text = "<think>我需要查询天气</think>北京今天晴天。"
        cleaned, _ = OutputSanitizer.sanitize(text)
        assert "<think>" not in cleaned
        assert "北京今天晴天" in cleaned

    def test_sanitize_tool_call_returns_empty(self):
        """整段为工具调用 JSON 时应清洗为空字符串。"""
        polluted = '{"name": "web_search", "arguments": {"query": "a"}}'
        cleaned, was_polluted = OutputSanitizer.sanitize(polluted)
        assert cleaned == ""
        assert was_polluted is True

    def test_sanitize_mixed_content(self):
        """包含工具调用 JSON 的行应被移除，污染状态应被标记。"""
        text = "根据搜索结果，[{\"name\": \"web_search\"}] 北京今天晴天。"
        cleaned, was_polluted = OutputSanitizer.sanitize(text)
        assert was_polluted is True
        assert "name" not in cleaned


# =============================================================================
# 2. 意图路由
# =============================================================================
class TestIntentRouter:
    """意图路由测试。"""

    @pytest.fixture
    def router(self):
        return IntentRouter()

    @pytest.mark.asyncio
    async def test_greeting(self, router):
        """问候语应直接走 LLM。"""
        decision = await router.route("你好")
        assert decision.primary_mode == PrimaryMode.DIRECT_LLM

    @pytest.mark.asyncio
    async def test_datetime_tool_first(self, router):
        """时间类问题应路由到时间工具。"""
        decision = await router.route("现在几点了")
        assert decision.primary_mode == PrimaryMode.TOOL_FIRST
        assert "get_current_time" in decision.suggested_tools

    @pytest.mark.asyncio
    async def test_weather_tool_first(self, router):
        """天气类问题应路由到天气工具。"""
        decision = await router.route("北京今天天气怎么样")
        assert decision.primary_mode == PrimaryMode.TOOL_FIRST
        assert "weather_query" in decision.suggested_tools
        assert decision.needs_realtime is True
        assert decision.fallback_strategy == FallbackStrategy.WEB_SEARCH

    @pytest.mark.asyncio
    async def test_calculation_tool_first(self, router):
        """计算类问题应路由到计算器工具。"""
        decision = await router.route("123 * 456 等于多少")
        assert decision.primary_mode == PrimaryMode.TOOL_FIRST
        assert "calculator" in decision.suggested_tools

    @pytest.mark.asyncio
    async def test_realtime_without_web_search(self, router):
        """未开启联网搜索的实时性问题应降级为直接 LLM。"""
        decision = await router.route("今天比特币价格")
        assert decision.needs_realtime is True
        assert decision.primary_mode == PrimaryMode.DIRECT_LLM

    @pytest.mark.asyncio
    async def test_realtime_with_web_search(self, router):
        """开启联网搜索的实时性问题应走联网搜索。"""
        decision = await router.route("今天比特币价格", use_web_search=True)
        assert decision.primary_mode == PrimaryMode.WEB_SEARCH
        assert decision.needs_web is True

    @pytest.mark.asyncio
    async def test_hybrid_with_kb_and_web(self, router):
        """有知识库且开启联网搜索应走混合模式。"""
        decision = await router.route("公司的休假制度", kb_ids=["kb-1"], use_web_search=True)
        assert decision.primary_mode == PrimaryMode.HYBRID
        assert decision.needs_kb is True
        assert decision.needs_web is True

    @pytest.mark.asyncio
    async def test_kb_only_preferred(self, router):
        """文档/制度类问题应优先使用知识库。"""
        decision = await router.route("根据文档第五条规定", kb_ids=["kb-1"])
        assert decision.primary_mode == PrimaryMode.KB_ONLY


# =============================================================================
# 3. 答案生成
# =============================================================================
class TestAnswerGenerator:
    """答案生成器测试。"""

    @pytest.fixture
    def mock_llm(self):
        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=MagicMock(content="mocked answer"))
        return llm

    @pytest.fixture
    def generator(self, mock_llm):
        return AnswerGenerator(mock_llm)

    @pytest.mark.asyncio
    async def test_generate_with_tool_results(self, generator, mock_llm):
        """基于工具结果生成答案。"""
        tool_results = [
            ToolResult(tool_name="weather_query", output="北京 25°C 晴", success=True)
        ]
        answer, sources = await generator.generate(
            question="北京天气",
            tool_results=tool_results,
            is_realtime=True,
        )
        assert answer == "mocked answer"
        assert "北京" in mock_llm.ainvoke.call_args[0][0]
        mock_llm.ainvoke.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_generate_no_context(self, generator, mock_llm):
        """无上下文时应使用 no_context 提示词。"""
        answer, sources = await generator.generate(
            question="你好",
            is_realtime=False,
        )
        assert answer == "mocked answer"
        prompt = mock_llm.ainvoke.call_args[0][0]
        assert "智能助手" in prompt


# =============================================================================
# 4. 链路追踪
# =============================================================================
class TestTraceCollector:
    """链路追踪测试。"""

    def test_trace_basic_info(self):
        """基础信息应正确记录。"""
        trace = TraceCollector(trace_id="trace-123")
        trace.set_basic(question="测试问题", session_id="session-1", user_id="user-1")
        data = trace.to_dict()
        assert data["id"] == "trace-123"
        assert data["question"] == "测试问题"
        assert data["session_id"] == "session-1"

    def test_trace_intent(self):
        """意图决策应可序列化。"""
        from src.services.intent_router import IntentDecision

        trace = TraceCollector()
        decision = IntentDecision(
            primary_mode=PrimaryMode.TOOL_FIRST,
            suggested_tools=["weather_query"],
        )
        trace.set_intent(decision)
        assert trace.to_dict()["primary_mode"] == "tool_first"
        assert trace.to_dict()["intent_decision"]["suggested_tools"] == ["weather_query"]

    def test_trace_tool_calls(self):
        """工具调用结果应可序列化。"""
        trace = TraceCollector()
        trace.set_tool_calls([ToolResult(tool_name="weather_query", output="25°C", success=True)])
        assert len(trace.to_dict()["tool_calls"]) == 1
        assert trace.to_dict()["tool_calls"][0]["tool_name"] == "weather_query"

    def test_trace_finish(self):
        """结束追踪时应计算耗时。"""
        trace = TraceCollector()
        trace.set_basic(question="q")
        trace.finish()
        assert trace.to_dict()["total_latency_ms"] >= 0


# =============================================================================
# 5. 搜索后处理
# =============================================================================
class TestSearchPostprocessor:
    """搜索后处理测试。"""

    @pytest.fixture
    def postprocessor(self):
        return SearchPostprocessor()

    def test_deduplicate_by_url(self, postprocessor):
        """相同 URL 应被去重。"""
        results = [
            SearchResult(title="t", url="http://example.com/a", content="c1"),
            SearchResult(title="t", url="http://example.com/a", content="c2"),
        ]
        deduped = postprocessor.deduplicate(results)
        assert len(deduped) == 1

    def test_deduplicate_by_content(self, postprocessor):
        """内容相同应被去重。"""
        results = [
            SearchResult(title="t1", url="http://example.com/a", content="same content"),
            SearchResult(title="t2", url="http://example.com/b", content="same content"),
        ]
        deduped = postprocessor.deduplicate(results)
        assert len(deduped) == 1

    def test_authority_score(self, postprocessor):
        """权威域名应获得更高分。"""
        gov = postprocessor._authority_score("https://www.gov.cn/policy")
        low = postprocessor._authority_score("https://example.com")
        assert gov > low

    def test_freshness_score(self, postprocessor):
        """包含较新年份的内容应获得更高时效分。"""
        fresh = postprocessor._freshness_score("2026年发布")
        stale = postprocessor._freshness_score("2024年发布")
        assert fresh > stale

    def test_process_rerank(self, postprocessor):
        """处理后结果应少于等于原始数量。"""
        results = [
            SearchResult(title="t", url=f"http://example.com/{i}", content=f"content {i}")
            for i in range(10)
        ]
        processed = postprocessor.process(results, top_k=5)
        assert len(processed) == 5


# =============================================================================
# 6. 工具插件
# =============================================================================
class TestToolPlugins:
    """工具插件单元测试。"""

    @pytest.mark.asyncio
    async def test_datetime_tool(self):
        """时间工具应返回当前时间字符串。"""
        tool = DateTimeTool()
        result = await tool.execute()
        assert result.success is True
        assert "202" in result.output  # 年份
        assert result.tool_name == "get_current_time"

    @pytest.mark.asyncio
    async def test_weather_tool_city_extraction(self):
        """天气工具应能提取城市名。"""
        assert _extract_city_name("北京今天天气") == "北京"
        assert _extract_city_name("上海的气温") == "上海"
        assert _extract_city_name("今天怎么样") is None

    @pytest.mark.asyncio
    async def test_calculator_tool(self):
        """计算器工具应正确计算表达式。"""
        tool = CalculatorTool()
        result = await tool.execute(expression="2 + 3 * 4")
        assert result.success is True
        assert "14" in result.output

    @pytest.mark.asyncio
    async def test_calculator_tool_invalid(self):
        """非法表达式应返回错误。"""
        tool = CalculatorTool()
        result = await tool.execute(expression="__import__('os')")
        assert result.success is False


# =============================================================================
# 7. 工具管理器
# =============================================================================
class TestToolManager:
    """工具管理平台测试。"""

    @pytest.mark.asyncio
    async def test_execute_registered_tool(self):
        """已注册工具应可执行。"""
        manager = ToolManager()
        manager.register_tool(CalculatorTool())
        result = await manager.execute("calculator", expression="1+1")
        assert result.success is True
        assert "2" in result.output

    @pytest.mark.asyncio
    async def test_execute_unknown_tool(self):
        """未注册工具应返回错误。"""
        manager = ToolManager()
        result = await manager.execute("unknown_tool")
        assert result.success is False
        assert "未注册" in result.error

    def test_discover_tools(self):
        """自动发现应能加载插件目录下的工具。"""
        manager = ToolManager()
        manager.discover_tools()
        names = [t.name for t in manager.registry.list_tools()]
        assert "calculator" in names
        assert "get_current_time" in names
        assert "weather_query" in names


# =============================================================================
# 8. 端到端场景（需要外部服务）
# =============================================================================
@pytest.mark.e2e
class TestEndToEndScenarios:
    """端到端场景测试，默认跳过，传入 --run-e2e 时运行。

    依赖：Ollama / PostgreSQL / Milvus / SearXNG / Open-Meteo 等外部服务。
    """

    @pytest.mark.asyncio
    async def test_datetime_e2e(self):
        """时间查询端到端。"""
        answer = build_datetime_answer("现在几点")
        assert answer is not None
        assert any(kw in answer for kw in ["年", "月", "日", ":"])

    @pytest.mark.asyncio
    async def test_weather_e2e(self):
        """天气查询端到端（需 Open-Meteo 网络访问）。"""
        tool = WeatherTool()
        result = await tool.execute(question="吕梁今天天气")
        assert result.success is True
        assert "°C" in result.output or "温度" in result.output

    @pytest.mark.asyncio
    async def test_knowledge_base_e2e(self):
        """知识库检索端到端（需 Milvus 向量库）。"""
        from src.services.rag_chain import RAGChain
        from src.services.vector_store import VectorStoreManager

        vector_store = VectorStoreManager()
        rag_chain = await RAGChain.get_instance(vector_store)
        # 需要已创建知识库
        answer, _, _, answer_type = await rag_chain.run("测试问题", kb_ids=["test-kb"])
        assert answer_type in ("knowledge_base", "hybrid_search")

    @pytest.mark.asyncio
    async def test_web_search_e2e(self):
        """联网搜索端到端（需 SearXNG 服务）。"""
        from src.services.web_search_service import WebSearchService

        service = WebSearchService()
        results = await service.search("2026 年新闻", max_results=3)
        assert len(results) > 0

    @pytest.mark.asyncio
    async def test_agent_mode_e2e(self):
        """Agent 模式端到端（需 Ollama Function Calling 能力）。"""
        from src.services.search_agent import SearchToolkit
        from src.services.web_search_service import WebSearchService

        service = WebSearchService()
        toolkit = SearchToolkit(service)
        result = await toolkit.tool_manager.execute("web_search", query="AI 新闻", reason="测试")
        assert result.success is True
