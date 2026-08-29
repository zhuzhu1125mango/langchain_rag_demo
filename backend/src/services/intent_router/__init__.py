"""意图路由模块。

根据用户问题和历史消息，判断问题类型并推荐合适的工具与问答模式，
为后续 ToolExecutor 和 AnswerGenerator 提供决策输入。

Phase 2 升级为三层架构：
- FastRuleRouter：规则快速路径，覆盖高频确定性场景
- LLMRouter：本地 LLM 语义理解层
- ConfidenceGate：置信度门控与环境适配
"""

import logging
from typing import Dict, List, Optional

from src.config import settings
from src.services.intent_router.confidence_gate import ConfidenceGate
from src.services.intent_router.conversation_context import (
    ConversationContext,
    ConversationContextBuilder,
    get_default_conversation_context_builder,
)
from src.services.intent_router.constants import (
    CALCULATION_KEYWORDS,
    COMPLEX_INTENT_KEYWORDS,
    FAST_PATH_MAX_QUESTION_LEN,
    GREETING_RE,
    KB_PREFERRED_KEYWORDS,
    MULTI_ENTITY_PATTERN,
    PRONOUN_PATTERN,
    REALTIME_KEYWORDS,
    RESEARCH_KEYWORDS,
    RULE_TEMPLATE_KEYWORDS,
)
from src.services.intent_router.embedding_classifier import EmbeddingIntentClassifier
from src.services.intent_router.llm_router import LLMRouter
from src.services.intent_router.models import (
    FallbackStrategy,
    IntentDecision,
    PrimaryMode,
    SearchPipeline,
)
from src.services.intent_router.tool_registry import (
    ToolMeta,
    ToolMetaRegistry,
    get_tool_meta_registry,
)
from src.services.tools.plugins._datetime_impl import is_datetime_question
from src.services.tools.plugins._weather_impl import _extract_city_name

logger = logging.getLogger("intent_router")

# 导出公共 API，保持旧导入路径兼容
__all__ = [
    "IntentRouter",
    "PrimaryMode",
    "FallbackStrategy",
    "SearchPipeline",
    "IntentDecision",
    "LLMRouter",
    "EmbeddingIntentClassifier",
    "ConfidenceGate",
    "ConversationContext",
    "ConversationContextBuilder",
    "get_default_conversation_context_builder",
    "ToolMeta",
    "ToolMetaRegistry",
    "get_tool_meta_registry",
]


class IntentRouter:
    """意图路由器：规则 fast-path + LLM 语义路由 + 置信度门控。"""

    def __init__(self):
        self._llm_router = LLMRouter()
        self._confidence_gate = ConfidenceGate()
        self._embedding_classifier = EmbeddingIntentClassifier()

    @staticmethod
    def is_greeting(question: str) -> bool:
        """判断是否为问候/日常对话。"""
        if not question:
            return False
        return bool(GREETING_RE.match(question.strip()))

    @staticmethod
    def is_realtime_question(question: str) -> bool:
        """判断是否为强时效性问题。"""
        if not question:
            return False
        q = question.lower()
        return any(kw in q for kw in REALTIME_KEYWORDS)

    @staticmethod
    def is_calculation_question(question: str) -> bool:
        """判断是否包含计算需求。"""
        if not question:
            return False
        return any(kw in question for kw in CALCULATION_KEYWORDS)

    @staticmethod
    def is_research_question(question: str) -> bool:
        """判断是否需要多步研究的复杂问题。"""
        if not question:
            return False
        return any(kw in question for kw in RESEARCH_KEYWORDS)

    @staticmethod
    def is_kb_preferred_question(question: str) -> bool:
        """判断是否更倾向于从知识库找答案。"""
        if not question:
            return False
        return any(kw in question for kw in KB_PREFERRED_KEYWORDS)

    @staticmethod
    def classify_search_pipeline(
        question: str,
        history: Optional[List[Dict]] = None,
    ) -> SearchPipeline:
        """判断联网搜索应走快速路径还是完整路径。

        快速路径（FAST_PATH）触发条件（命中任一）：
        - 问题 < 15 字。
        - 命中规则模板关键词（时间/地点/价格/天气）。
        - 非多实体问题（不含 "和/与/vs/哪个"）。
        - 非多轮对话追问（无代词/省略实体，或无历史）。

        完整路径（FULL_PATH）触发条件：
        - 含复杂研究意图关键词（对比/分析/调研/最新进展等）。
        - 多实体对比（A 和/与/vs B）。
        - 多轮对话追问（含代词且有历史）。
        """
        if not question:
            return SearchPipeline.FAST_PATH

        if any(kw in question for kw in COMPLEX_INTENT_KEYWORDS):
            return SearchPipeline.FULL_PATH

        if MULTI_ENTITY_PATTERN.search(question):
            return SearchPipeline.FULL_PATH

        if history and PRONOUN_PATTERN.search(question):
            return SearchPipeline.FULL_PATH

        if any(kw in question for kw in RULE_TEMPLATE_KEYWORDS):
            return SearchPipeline.FAST_PATH

        if len(question) < FAST_PATH_MAX_QUESTION_LEN:
            return SearchPipeline.FAST_PATH

        return SearchPipeline.FULL_PATH

    @staticmethod
    def is_price_question(question: str) -> bool:
        """识别是否为贵金属价格类查询。"""
        if not question:
            return False
        q = question.lower()
        price_keywords = {
            "金价", "黄金价格", "白银价格", "银价", "铂金价格", "钯金价格",
            "黄金", "白银", "铂金", "钯金", "贵金属",
        }
        return any(kw in q for kw in price_keywords)

    @staticmethod
    def is_exchange_rate_question(question: str) -> bool:
        """识别是否为汇率/兑换类查询。"""
        if not question:
            return False
        q = question.lower()
        rate_keywords = {
            "汇率", "兑换", "换算", "外汇",
            "美元兑人民币", "美元等于多少人民币", "美元换人民币",
            "欧元兑人民币", "日元兑人民币", "港币兑人民币", "人民币兑美元",
        }
        import re

        pattern = re.compile(
            r"\d*\s*[a-zA-Z]{3}\s*(?:to|兑|兑换|换算成|等于|换|转)\s*[a-zA-Z]{3}",
            re.IGNORECASE,
        )
        if pattern.search(q):
            return True
        return any(kw in q for kw in rate_keywords)

    def _detect_rule_conflict(self, question: str) -> bool:
        """检测多个规则同时命中，视为冲突需转 LLM 裁决。

        注意：实时性问题优先级较高，仅当同时出现明显的 Agent 多步研究意图
        （如对比/调研/总结/优缺点/区别）时才视为冲突，避免"分析最新新闻"这类
        问题被过度路由到 LLM。
        """
        if not settings.intent_router.INTENT_ROUTER_RULE_CONFLICT_DETECTION:
            return False

        realtime_hit = self.is_realtime_question(question)
        calc_hit = self.is_calculation_question(question)
        kb_hit = self.is_kb_preferred_question(question)
        research_hit = self.is_research_question(question)

        # 强研究意图关键词，命中后确实需要 LLM 裁决
        strong_research_keywords = {"对比", "比较", "调研", "总结", "优缺点", "区别"}
        strong_research_hit = any(kw in question for kw in strong_research_keywords)

        # 实时性 + 强研究意图 → 冲突（如"对比今天和昨天的新闻"）
        if realtime_hit and strong_research_hit:
            return True

        # 实时性/计算/知识库两两交叉 → 冲突
        non_research_hits = sum([realtime_hit, calc_hit, kb_hit])
        if non_research_hits >= 2:
            return True

        # 知识库 + 普通研究意图 → 冲突
        if kb_hit and research_hit:
            return True

        return False

    async def route(
        self,
        question: str,
        kb_ids: Optional[List[str]] = None,
        use_web_search: bool = False,
        search_mode: str = "simple",
        history: Optional[List[Dict]] = None,
    ) -> IntentDecision:
        """根据问题路由到合适的处理模式与工具。

        流程：规则 fast-path → 规则冲突/未命中 → LLM 语义路由 → 置信度门控。

        Args:
            question: 用户问题。
            kb_ids: 已选知识库 ID 列表。
            use_web_search: 是否开启联网搜索。
            search_mode: 前端传入的搜索模式（simple/function_calling/agent）。
            history: 对话历史（可选）。

        Returns:
            IntentDecision: 意图决策结果。
        """
        question = (question or "").strip()
        has_kb = bool(kb_ids and len(kb_ids) > 0)

        # 1. 问候/日常对话
        if self.is_greeting(question):
            return IntentDecision(
                primary_mode=PrimaryMode.DIRECT_LLM,
                reasoning="问候/日常对话，直接由 LLM 回答",
                rule_hit=True,
            )

        # 2. 时间/日期类问题 -> 专用工具
        if is_datetime_question(question):
            return IntentDecision(
                primary_mode=PrimaryMode.TOOL_FIRST,
                suggested_tools=["get_current_time"],
                fallback_strategy=FallbackStrategy.LLM_DIRECT,
                reasoning="时间/日期类问题，使用系统时间工具",
                rule_hit=True,
            )

        # 3. 天气类问题 -> 专用工具
        if _extract_city_name(question):
            return IntentDecision(
                needs_realtime=True,
                primary_mode=PrimaryMode.TOOL_FIRST,
                suggested_tools=["weather_query"],
                fallback_strategy=FallbackStrategy.WEB_SEARCH,
                reasoning="识别到城市名，优先使用天气工具",
                rule_hit=True,
            )

        # 4. 汇率类问题 -> 汇率工具
        if self.is_exchange_rate_question(question):
            return IntentDecision(
                needs_realtime=True,
                primary_mode=PrimaryMode.TOOL_FIRST,
                suggested_tools=["exchange_rate"],
                fallback_strategy=FallbackStrategy.WEB_SEARCH,
                reasoning="识别到汇率/兑换查询，优先使用汇率工具",
                rule_hit=True,
            )

        # 5. 贵金属价格类问题 -> 金价工具
        if self.is_price_question(question):
            return IntentDecision(
                needs_realtime=True,
                primary_mode=PrimaryMode.TOOL_FIRST,
                suggested_tools=["gold_price"],
                fallback_strategy=FallbackStrategy.WEB_SEARCH,
                reasoning="识别到贵金属价格查询，优先使用金价工具",
                rule_hit=True,
            )

        # 6. 计算类问题 -> 计算器工具
        if self.is_calculation_question(question):
            return IntentDecision(
                primary_mode=PrimaryMode.TOOL_FIRST,
                suggested_tools=["calculator"],
                fallback_strategy=FallbackStrategy.LLM_DIRECT,
                reasoning="识别到计算需求，使用计算器工具",
                rule_hit=True,
            )

        # 7. 规则冲突检测：多个规则同时命中时转 LLM 裁决
        if self._detect_rule_conflict(question):
            if settings.intent_router.INTENT_ROUTER_USE_LLM:
                try:
                    draft = await self._llm_router.route(question, history=history)
                    return self._confidence_gate.apply(
                        draft,
                        question,
                        has_kb=has_kb,
                        use_web_search=use_web_search,
                        search_mode=search_mode,
                    )
                except Exception as e:
                    logger.warning(f"LLM 路由冲突裁决失败，降级为直接 LLM: {e}")
                    return IntentDecision(
                        primary_mode=PrimaryMode.DIRECT_LLM,
                        fallback_strategy=FallbackStrategy.NONE,
                        reasoning="规则冲突且 LLM 裁决失败，降级为直接回答",
                        rule_hit=True,
                    )

        # 8. 强时效性问题 -> 优先联网搜索
        if self.is_realtime_question(question):
            if has_kb and use_web_search:
                pipeline = self.classify_search_pipeline(question, history)
                return IntentDecision(
                    needs_realtime=True,
                    needs_kb=True,
                    needs_web=True,
                    primary_mode=PrimaryMode.HYBRID,
                    suggested_tools=["web_search"],
                    fallback_strategy=FallbackStrategy.TELL_FAILURE,
                    reasoning="强时效性问题，需要联网+知识库混合",
                    search_pipeline=pipeline,
                    rule_hit=True,
                )
            if use_web_search:
                pipeline = self.classify_search_pipeline(question, history)
                return IntentDecision(
                    needs_realtime=True,
                    needs_web=True,
                    primary_mode=PrimaryMode.WEB_SEARCH,
                    suggested_tools=["web_search"],
                    fallback_strategy=FallbackStrategy.TELL_FAILURE,
                    reasoning="强时效性问题，使用联网搜索",
                    search_pipeline=pipeline,
                    rule_hit=True,
                )
            return IntentDecision(
                needs_realtime=True,
                primary_mode=PrimaryMode.DIRECT_LLM,
                fallback_strategy=FallbackStrategy.TELL_FAILURE,
                reasoning="强时效性问题但未开启联网搜索，由 LLM 说明无法获取实时信息",
                rule_hit=True,
            )

        # 9. 复杂研究性问题 -> Agent 模式（如果开启）
        if self.is_research_question(question) and search_mode in ("function_calling", "agent"):
            return IntentDecision(
                needs_web=use_web_search,
                primary_mode=PrimaryMode.AGENT_RESEARCH,
                suggested_tools=["web_search", "fetch_webpage"],
                fallback_strategy=FallbackStrategy.WEB_SEARCH,
                reasoning="复杂研究性问题，使用 Agent 多步搜索",
                rule_hit=True,
            )

        # 10. 知识库优先 / 有知识库
        if has_kb:
            if use_web_search:
                pipeline = self.classify_search_pipeline(question, history)
                return IntentDecision(
                    needs_kb=True,
                    needs_web=True,
                    primary_mode=PrimaryMode.HYBRID,
                    suggested_tools=["web_search"],
                    fallback_strategy=FallbackStrategy.LLM_DIRECT,
                    reasoning="有知识库且开启联网搜索，使用混合模式",
                    search_pipeline=pipeline,
                    rule_hit=True,
                )
            if self.is_kb_preferred_question(question):
                return IntentDecision(
                    needs_kb=True,
                    primary_mode=PrimaryMode.KB_ONLY,
                    fallback_strategy=FallbackStrategy.LLM_DIRECT,
                    reasoning="问题涉及文档/制度/流程，优先查知识库",
                    rule_hit=True,
                )
            return IntentDecision(
                needs_kb=True,
                primary_mode=PrimaryMode.HYBRID,
                fallback_strategy=FallbackStrategy.LLM_DIRECT,
                reasoning="有知识库，使用知识库+LLM混合",
                rule_hit=True,
            )

        # 11. Embedding 语义分类层（规则未命中时作为 LLM 路由前的轻量补充）
        if getattr(settings.intent_router, "INTENT_ROUTER_USE_EMBEDDING", True):
            try:
                emb_result = await self._embedding_classifier.classify(question)
                if emb_result.confidence >= self._embedding_classifier.similarity_threshold:
                    emb_decision = self._embedding_classifier.to_intent_decision(
                        emb_result, question, has_kb=has_kb, use_web_search=use_web_search
                    )
                    logger.info(
                        f"Embedding 意图分类命中: mode={emb_decision.primary_mode.value}, "
                        f"confidence={emb_result.confidence:.3f}"
                    )
                    return emb_decision
            except Exception as e:
                logger.warning(f"Embedding 意图分类失败，继续 LLM 路由: {e}")

        # 12. 规则未命中且开启 LLM 路由 → 走语义路由
        if settings.intent_router.INTENT_ROUTER_USE_LLM:
            try:
                draft = await self._llm_router.route(question, history=history)
                return self._confidence_gate.apply(
                    draft,
                    question,
                    has_kb=has_kb,
                    use_web_search=use_web_search,
                    search_mode=search_mode,
                )
            except Exception as e:
                logger.warning(f"LLM 路由失败，降级为直接 LLM: {e}")
                return IntentDecision(
                    primary_mode=PrimaryMode.DIRECT_LLM,
                    fallback_strategy=FallbackStrategy.NONE,
                    reasoning="LLM 路由失败，降级为直接回答",
                    rule_hit=True,
                )

        # 13. 默认直接 LLM
        return IntentDecision(
            primary_mode=PrimaryMode.DIRECT_LLM,
            fallback_strategy=FallbackStrategy.NONE,
            reasoning="无需检索/工具，直接由 LLM 回答",
            rule_hit=True,
        )
