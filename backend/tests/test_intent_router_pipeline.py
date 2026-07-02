"""IntentRouter 分级流水线分类单元测试。

覆盖 P1 新增的 classify_search_pipeline 方法与 SearchPipeline 枚举，
验证快速路径/完整路径的触发条件符合设计文档 4.1/4.2 节定义。
"""

import pytest

from src.services.intent_router import (
    IntentRouter,
    IntentDecision,
    PrimaryMode,
    SearchPipeline,
)


class TestSearchPipelineEnum:
    """SearchPipeline 枚举基础测试。"""

    def test_fast_path_value(self):
        assert SearchPipeline.FAST_PATH.value == "fast_path"

    def test_full_path_value(self):
        assert SearchPipeline.FULL_PATH.value == "full_path"

    def test_enum_comparison(self):
        assert SearchPipeline.FAST_PATH != SearchPipeline.FULL_PATH


class TestClassifySearchPipelineFastPath:
    """快速路径触发条件测试。"""

    def setup_method(self):
        self.router = IntentRouter()

    def test_empty_question_returns_fast(self):
        assert self.router.classify_search_pipeline("") == SearchPipeline.FAST_PATH

    def test_none_question_returns_fast(self):
        assert self.router.classify_search_pipeline(None) == SearchPipeline.FAST_PATH

    def test_short_question_returns_fast(self):
        # < 15 字
        assert self.router.classify_search_pipeline("金价多少") == SearchPipeline.FAST_PATH

    def test_rule_template_time_keyword(self):
        assert self.router.classify_search_pipeline("今天比特币价格走势如何") == SearchPipeline.FAST_PATH

    def test_rule_template_price_keyword(self):
        assert self.router.classify_search_pipeline("现在黄金多少钱一克") == SearchPipeline.FAST_PATH

    def test_rule_template_weather_keyword(self):
        assert self.router.classify_search_pipeline("北京天气情况") == SearchPipeline.FAST_PATH

    def test_rule_template_latest_keyword(self):
        assert self.router.classify_search_pipeline("最新科技新闻") == SearchPipeline.FAST_PATH

    def test_no_history_no_pronoun_medium_length_fast(self):
        # 15 字以上、无规则关键词、无代词、无历史 → 默认完整路径
        # 但此处验证：命中规则关键词仍走快速路径
        assert self.router.classify_search_pipeline("今日金价多少元一克") == SearchPipeline.FAST_PATH


class TestClassifySearchPipelineFullPath:
    """完整路径触发条件测试。"""

    def setup_method(self):
        self.router = IntentRouter()

    def test_complex_intent_keyword_analysis(self):
        assert self.router.classify_search_pipeline("分析一下当前经济形势") == SearchPipeline.FULL_PATH

    def test_complex_intent_keyword_research(self):
        assert self.router.classify_search_pipeline("调研新能源汽车市场") == SearchPipeline.FULL_PATH

    def test_complex_intent_keyword_compare(self):
        assert self.router.classify_search_pipeline("对比两款手机的性能") == SearchPipeline.FULL_PATH

    def test_complex_intent_keyword_summary(self):
        assert self.router.classify_search_pipeline("总结一下本次会议要点") == SearchPipeline.FULL_PATH

    def test_multi_entity_pattern(self):
        # 含 "A 和 B" 模式
        assert self.router.classify_search_pipeline("iPhone和安卓手机哪个好") == SearchPipeline.FULL_PATH

    def test_multi_entity_pattern_vs(self):
        assert self.router.classify_search_pipeline("Python vs Java性能对比") == SearchPipeline.FULL_PATH

    def test_multi_entity_pattern_yu(self):
        assert self.router.classify_search_pipeline("React与Vue区别") == SearchPipeline.FULL_PATH

    def test_pronoun_with_history(self):
        history = [{"role": "user", "content": "金价多少"}, {"role": "assistant", "content": "约780元"}]
        # 含代词"它" + 有历史 → 完整路径
        assert self.router.classify_search_pipeline("它未来走势如何", history=history) == SearchPipeline.FULL_PATH

    def test_pronoun_this_with_history(self):
        history = [{"role": "user", "content": "介绍下Python"}]
        assert self.router.classify_search_pipeline("这个怎么用", history=history) == SearchPipeline.FULL_PATH

    def test_pronoun_without_history_still_fast_if_rule_keyword(self):
        # 含代词但无历史，且命中规则关键词 → 快速路径（代词需+历史才触发完整路径）
        assert self.router.classify_search_pipeline("它今天价格") == SearchPipeline.FAST_PATH

    def test_default_full_path_medium_length_no_rule(self):
        # 15 字以上、无规则关键词、无代词、无多实体 → 默认完整路径
        assert self.router.classify_search_pipeline("请详细介绍量子计算的基本原理与应用前景") == SearchPipeline.FULL_PATH


class TestRouteSetsSearchPipeline:
    """route 方法是否正确设置 search_pipeline 字段。"""

    def setup_method(self):
        self.router = IntentRouter()

    @pytest.mark.asyncio
    async def test_route_greeting_no_pipeline(self):
        # 问候类，search_pipeline 保持默认 FAST_PATH（不影响）
        decision = await self.router.route("你好")
        assert decision.primary_mode == PrimaryMode.DIRECT_LLM
        assert decision.search_pipeline == SearchPipeline.FAST_PATH

    @pytest.mark.asyncio
    async def test_route_web_search_fast_path(self):
        # 强时效性 + 开启联网 → WEB_SEARCH（非工具优先），命中规则关键词 → 快速路径
        # "最新科技新闻" 含 "最新"/"新闻" 属实时性，但不命中价格/天气/汇率工具
        decision = await self.router.route("最新科技新闻", use_web_search=True)
        assert decision.primary_mode == PrimaryMode.WEB_SEARCH
        assert decision.search_pipeline == SearchPipeline.FAST_PATH

    @pytest.mark.asyncio
    async def test_route_web_search_full_path_complex(self):
        # 强时效性 + 复杂意图 → WEB_SEARCH，完整路径
        # "分析最新科技新闻" 含 "新闻"(实时关键词) + "分析"(复杂意图)
        decision = await self.router.route("分析最新科技新闻", use_web_search=True)
        assert decision.primary_mode == PrimaryMode.WEB_SEARCH
        assert decision.search_pipeline == SearchPipeline.FULL_PATH

    @pytest.mark.asyncio
    async def test_route_hybrid_with_history_pronoun(self):
        # 有知识库 + 联网 + 多轮追问 → HYBRID，完整路径
        history = [{"role": "user", "content": "介绍Python"}, {"role": "assistant", "content": "..."}]
        decision = await self.router.route(
            "它和Java对比怎么样",
            kb_ids=["kb1"],
            use_web_search=True,
            history=history,
        )
        assert decision.primary_mode == PrimaryMode.HYBRID
        assert decision.search_pipeline == SearchPipeline.FULL_PATH

    @pytest.mark.asyncio
    async def test_route_tool_first_not_affected(self):
        # 工具优先模式（天气/价格/计算）不走搜索流水线，pipeline 保持默认
        decision = await self.router.route("北京天气")
        assert decision.primary_mode == PrimaryMode.TOOL_FIRST
        assert decision.search_pipeline == SearchPipeline.FAST_PATH

    @pytest.mark.asyncio
    async def test_decision_to_dict_includes_pipeline(self):
        decision = await self.router.route("最新科技新闻", use_web_search=True)
        d = decision.to_dict()
        assert "search_pipeline" in d
        assert d["search_pipeline"] == "fast_path"


class TestEdgeCases:
    """边界情况测试。"""

    def setup_method(self):
        self.router = IntentRouter()

    def test_exactly_15_chars_boundary(self):
        # 15 字边界：刚好 15 字不算短问题（< 15 才算）
        question = "一二三四五六七八九十一二三四"  # 14 字
        # 无规则关键词、无多实体、无代词 → 14 字 < 15 → 快速路径
        assert self.router.classify_search_pipeline(question) == SearchPipeline.FAST_PATH

    def test_15_chars_no_rule_full_path(self):
        # 15 字且无规则关键词 → 完整路径
        question = "请介绍量子计算的基本原理"  # 12 字... 实际计数
        # 这个问题 12 字 < 15，应该是快速路径
        # 换一个 >= 15 字的
        question = "请详细介绍一下量子计算的基本原理和应用"  # 19 字
        result = self.router.classify_search_pipeline(question)
        assert result == SearchPipeline.FULL_PATH

    def test_pronoun_alone_no_history_fast(self):
        # 仅含代词但无历史，且问题较短 → 快速路径
        assert self.router.classify_search_pipeline("它") == SearchPipeline.FAST_PATH

    def test_history_empty_list_treated_as_no_history(self):
        # 空历史列表等同于无历史
        assert self.router.classify_search_pipeline("它怎么样", history=[]) == SearchPipeline.FAST_PATH

    def test_compare_keyword_overrides_short_length(self):
        # "对比" 是复杂意图关键词，即使短问题也走完整路径
        assert self.router.classify_search_pipeline("对比A和B") == SearchPipeline.FULL_PATH
