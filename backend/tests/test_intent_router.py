"""意图路由模块单元测试。

覆盖规则路由、LLM 输出解析、置信度门控以及 IntentRouter 的三层决策流程。
"""

import pytest

from src.services.intent_router import (
    ConfidenceGate,
    ConversationContextBuilder,
    FallbackStrategy,
    IntentDecision,
    IntentRouter,
    LLMRouter,
    PrimaryMode,
    SearchPipeline,
    ToolMeta,
    ToolMetaRegistry,
)


class TestRuleRouter:
    """规则路由静态方法测试。"""

    def test_is_greeting(self):
        assert IntentRouter.is_greeting("你好")
        assert IntentRouter.is_greeting("hello")
        assert not IntentRouter.is_greeting("今天天气怎么样")

    def test_is_realtime_question(self):
        assert IntentRouter.is_realtime_question("今天金价多少")
        assert IntentRouter.is_realtime_question("北京天气")
        assert not IntentRouter.is_realtime_question("介绍一下你自己")

    def test_is_calculation_question(self):
        assert IntentRouter.is_calculation_question("1+1等于几")
        assert IntentRouter.is_calculation_question("15% 乘以 200 是多少")
        assert not IntentRouter.is_calculation_question("今天星期几")

    def test_is_price_question(self):
        assert IntentRouter.is_price_question("今天金价")
        assert IntentRouter.is_price_question("白银价格")
        assert not IntentRouter.is_price_question("今天天气")

    def test_is_exchange_rate_question(self):
        assert IntentRouter.is_exchange_rate_question("美元兑人民币汇率")
        assert IntentRouter.is_exchange_rate_question("100 USD to CNY")
        assert not IntentRouter.is_exchange_rate_question("今天金价")

    def test_classify_search_pipeline(self):
        assert IntentRouter.classify_search_pipeline("今天天气") == SearchPipeline.FAST_PATH
        assert IntentRouter.classify_search_pipeline("A 和 B 的区别") == SearchPipeline.FULL_PATH
        assert IntentRouter.classify_search_pipeline("它怎么样", history=[{"role": "user", "content": "A"}]) == SearchPipeline.FULL_PATH


class TestLLMRouter:
    """LLM 路由输出解析测试。"""

    def test_parse_llm_output_basic(self):
        output = '''{"needs_kb": 0.1, "needs_web": 0.9, "needs_realtime": 0.95, "needs_tool": 0.0,
                     "primary_mode": "web_search", "suggested_tools": ["web_search"],
                     "search_pipeline": "fast_path", "needs_clarify": false,
                     "context_rewrite": "2026年6月28日黄金价格", "reasoning": "实时性问题"}'''
        router = LLMRouter()
        decision = router._parse_llm_output(output)

        assert decision.primary_mode == PrimaryMode.WEB_SEARCH
        assert decision.confidence_scores["needs_web"] == pytest.approx(0.9)
        assert decision.suggested_tools == ["web_search"]
        assert decision.search_pipeline == SearchPipeline.FAST_PATH
        assert decision.context_rewrite == "2026年6月28日黄金价格"
        assert decision.llm_routed is True

    def test_parse_llm_output_with_markdown(self):
        output = "```json\n{\"primary_mode\": \"direct_llm\", \"needs_kb\": 0.0}\n```"
        router = LLMRouter()
        decision = router._parse_llm_output(output)
        assert decision.primary_mode == PrimaryMode.DIRECT_LLM

    def test_parse_llm_output_invalid_defaults(self):
        output = "{\"primary_mode\": \"unknown_mode\", \"search_pipeline\": \"unknown\"}"
        router = LLMRouter()
        decision = router._parse_llm_output(output)
        assert decision.primary_mode == PrimaryMode.DIRECT_LLM
        assert decision.search_pipeline == SearchPipeline.FAST_PATH

    def test_safe_float_clamping(self):
        router = LLMRouter()
        assert router._safe_float(-0.5) == 0.0
        assert router._safe_float(1.5) == 1.0
        assert router._safe_float("abc", 0.3) == 0.3


class TestConfidenceGate:
    """置信度门控测试。"""

    @pytest.fixture
    def gate(self):
        return ConfidenceGate()

    def test_low_confidence_to_direct_llm(self, gate):
        draft = IntentDecision(
            confidence_scores={"needs_kb": 0.1, "needs_web": 0.1},
            primary_mode=PrimaryMode.HYBRID,
            llm_routed=True,
        )
        result = gate.apply(draft, "模糊问题")
        assert result.primary_mode == PrimaryMode.DIRECT_LLM
        assert "置信度过低" in result.reasoning

    def test_ambiguity_triggers_clarify(self, gate):
        draft = IntentDecision(
            confidence_scores={"needs_kb": 0.6, "needs_web": 0.55},
            primary_mode=PrimaryMode.HYBRID,
            llm_routed=True,
        )
        result = gate.apply(draft, "帮我查一下这个")
        assert result.needs_clarify is True
        assert result.clarify_question != ""

    def test_kb_without_collection_fallback_to_web(self, gate):
        draft = IntentDecision(
            confidence_scores={"needs_kb": 0.9, "needs_web": 0.2},
            primary_mode=PrimaryMode.KB_ONLY,
            llm_routed=True,
        )
        result = gate.apply(draft, "文档里怎么说的", has_kb=False)
        assert result.needs_web is True
        assert result.primary_mode == PrimaryMode.WEB_SEARCH

    def test_realtime_without_web_search(self, gate):
        draft = IntentDecision(
            confidence_scores={"needs_realtime": 0.9, "needs_web": 0.8},
            primary_mode=PrimaryMode.WEB_SEARCH,
            llm_routed=True,
        )
        result = gate.apply(draft, "今天新闻", use_web_search=False)
        assert result.fallback_strategy == FallbackStrategy.TELL_FAILURE

    def test_context_rewrite_fallback_to_question(self, gate):
        draft = IntentDecision(
            confidence_scores={"needs_web": 0.9},
            primary_mode=PrimaryMode.WEB_SEARCH,
            llm_routed=True,
        )
        result = gate.apply(draft, "原始问题")
        assert result.context_rewrite == "原始问题"


class TestIntentRouterIntegration:
    """IntentRouter 三层流程集成测试。"""

    @pytest.mark.asyncio
    async def test_rule_fast_path_greeting(self):
        router = IntentRouter()
        decision = await router.route("你好")
        assert decision.primary_mode == PrimaryMode.DIRECT_LLM
        assert decision.rule_hit is True
        assert decision.llm_routed is False

    @pytest.mark.asyncio
    async def test_rule_fast_path_weather(self):
        router = IntentRouter()
        decision = await router.route("北京今天天气")
        assert decision.primary_mode == PrimaryMode.TOOL_FIRST
        assert "weather_query" in decision.suggested_tools
        assert decision.rule_hit is True

    @pytest.mark.asyncio
    async def test_rule_fast_path_calculation(self):
        router = IntentRouter()
        decision = await router.route("1+1等于几")
        assert decision.primary_mode == PrimaryMode.TOOL_FIRST
        assert "calculator" in decision.suggested_tools

    @pytest.mark.asyncio
    async def test_llm_path_when_rule_miss(self, monkeypatch):
        router = IntentRouter()

        # 关闭 Embedding 分类层，确保命中 LLM 路由 mock
        monkeypatch.setattr(
            "src.services.intent_router.settings.intent_router.INTENT_ROUTER_USE_EMBEDDING",
            False,
        )

        async def mock_llm_route(question, history=None):
            return IntentDecision(
                confidence_scores={"needs_web": 0.9, "needs_realtime": 0.8},
                primary_mode=PrimaryMode.WEB_SEARCH,
                suggested_tools=["web_search"],
                search_pipeline=SearchPipeline.FAST_PATH,
                reasoning="模拟 LLM 决策",
                llm_routed=True,
            )

        monkeypatch.setattr(router._llm_router, "route", mock_llm_route)

        decision = await router.route("帮我查下这个", use_web_search=True)
        assert decision.primary_mode == PrimaryMode.WEB_SEARCH
        assert decision.llm_routed is True
        assert decision.needs_web is True

    @pytest.mark.asyncio
    async def test_llm_failure_fallback(self, monkeypatch):
        router = IntentRouter()

        # 关闭 Embedding 分类层，确保命中 LLM 路由并触发失败降级
        monkeypatch.setattr(
            "src.services.intent_router.settings.intent_router.INTENT_ROUTER_USE_EMBEDDING",
            False,
        )

        async def mock_llm_route(question, history=None):
            raise RuntimeError("模拟 LLM 失败")

        monkeypatch.setattr(router._llm_router, "route", mock_llm_route)

        decision = await router.route("一个规则没覆盖的问题")
        assert decision.primary_mode == PrimaryMode.DIRECT_LLM
        assert "LLM 路由失败" in decision.reasoning

    @pytest.mark.asyncio
    async def test_llm_disabled_uses_rule_default(self, monkeypatch):
        router = IntentRouter()
        monkeypatch.setattr(
            "src.services.intent_router.settings.intent_router.INTENT_ROUTER_USE_LLM",
            False,
        )
        monkeypatch.setattr(
            "src.services.intent_router.settings.intent_router.INTENT_ROUTER_USE_EMBEDDING",
            False,
        )

        decision = await router.route("一个规则没覆盖的问题")
        assert decision.primary_mode == PrimaryMode.DIRECT_LLM
        assert decision.llm_routed is False

    @pytest.mark.asyncio
    async def test_context_rewrite_used_from_llm(self, monkeypatch):
        router = IntentRouter()

        # 关闭 Embedding 分类层，确保命中 LLM 路由 mock
        monkeypatch.setattr(
            "src.services.intent_router.settings.intent_router.INTENT_ROUTER_USE_EMBEDDING",
            False,
        )

        async def mock_llm_route(question, history=None):
            return IntentDecision(
                confidence_scores={"needs_kb": 0.9},
                primary_mode=PrimaryMode.KB_ONLY,
                context_rewrite="黄金价格今天多少",
                reasoning="改写后的问题",
                llm_routed=True,
            )

        monkeypatch.setattr(router._llm_router, "route", mock_llm_route)

        decision = await router.route("帮我查下这个", use_web_search=True, history=[
            {"role": "user", "content": "黄金价格"},
        ])
        assert decision.context_rewrite == "黄金价格今天多少"
        assert decision.llm_routed is True


class TestConversationContext:
    """ConversationContextBuilder 单元测试。"""

    def test_no_history_returns_original(self):
        builder = ConversationContextBuilder()
        ctx = builder.build("今天天气怎么样", history=[])
        assert ctx.resolved_question == "今天天气怎么样"
        assert ctx.compressed_history == ""
        assert ctx.has_resolution is False

    def test_compress_history_truncates(self):
        builder = ConversationContextBuilder(max_history_turns=2, max_history_chars=50)
        history = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好，有什么可以帮您的？"},
            {"role": "user", "content": "北京天气"},
            {"role": "assistant", "content": "北京今天晴。"},
        ]
        ctx = builder.build("上海呢", history=history)
        assert "北京" in ctx.compressed_history
        assert "你好" not in ctx.compressed_history

    def test_resolve_pronoun_with_previous_topic(self):
        builder = ConversationContextBuilder()
        history = [
            {"role": "user", "content": "黄金价格多少"},
            {"role": "assistant", "content": "今天黄金价格为 780 元/克。"},
        ]
        ctx = builder.build("它今天涨了吗", history=history)
        assert "黄金" in ctx.resolved_question
        assert ctx.has_resolution is True

    def test_resolve_pronoun_no_topic_fallback(self):
        builder = ConversationContextBuilder()
        history = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好。"},
        ]
        ctx = builder.build("它多少钱", history=history)
        assert ctx.resolved_question == "它多少钱"
        assert ctx.has_resolution is False


class TestToolMetaRegistry:
    """工具元数据注册表测试。"""

    def test_default_tools_registered(self):
        registry = ToolMetaRegistry()
        names = registry.names()
        assert "gold_price" in names
        assert "exchange_rate" in names
        assert "weather_query" in names

    def test_select_tools_by_priority(self):
        registry = ToolMetaRegistry()
        result = registry.select_tools("今天黄金价格多少")
        assert "gold_price" in result
        assert "web_search" not in result[:2]  # 高优先级工具排在前面

    def test_resolve_conflict_entity_complete(self):
        registry = ToolMetaRegistry()
        result = registry.resolve_conflict("美元兑人民币汇率", ["gold_price", "exchange_rate"])
        assert result == ["exchange_rate"]

    def test_entity_completeness(self):
        registry = ToolMetaRegistry()
        assert registry.entity_completeness("北京天气", "weather_query") == 1.0
        assert registry.entity_completeness("空气怎么样", "weather_query") == 0.0

    def test_custom_tool_registration(self):
        registry = ToolMetaRegistry()
        registry.register(ToolMeta(
            name="custom_tool",
            description="自定义工具",
            trigger_patterns=["custom"],
            conflict_priority=3,
        ))
        assert registry.get("custom_tool") is not None


class TestConfidenceGateToolFiltering:
    """ConfidenceGate 工具过滤测试。"""

    @pytest.fixture
    def gate(self):
        return ConfidenceGate()

    def test_filter_removes_incomplete_tool(self, gate):
        draft = IntentDecision(
            confidence_scores={"needs_tool": 0.9},
            primary_mode=PrimaryMode.TOOL_FIRST,
            suggested_tools=["weather_query"],
            llm_routed=True,
        )
        result = gate.apply(draft, "空气怎么样")  # 缺少城市实体
        assert "weather_query" not in result.suggested_tools

    def test_filter_keeps_complete_tool(self, gate):
        draft = IntentDecision(
            confidence_scores={"needs_tool": 0.9},
            primary_mode=PrimaryMode.TOOL_FIRST,
            suggested_tools=["weather_query"],
            llm_routed=True,
        )
        result = gate.apply(draft, "北京天气怎么样")
        assert "weather_query" in result.suggested_tools

    def test_filter_keeps_unknown_tool(self, gate):
        draft = IntentDecision(
            confidence_scores={"needs_tool": 0.9},
            primary_mode=PrimaryMode.TOOL_FIRST,
            suggested_tools=["future_tool"],
            llm_routed=True,
        )
        result = gate.apply(draft, "任意问题")
        assert "future_tool" in result.suggested_tools
