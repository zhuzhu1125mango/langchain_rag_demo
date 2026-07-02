"""置信度门控与决策后处理。

对 LLMRouter 输出的意图草案进行环境适配、歧义检测、降级与兜底处理，
输出最终可执行的 IntentDecision。
"""

import logging
from typing import Dict, List, Optional

from src.config import settings
from src.services.intent_router.models import (
    FallbackStrategy,
    IntentDecision,
    PrimaryMode,
)
from src.services.intent_router.tool_registry import get_tool_meta_registry

logger = logging.getLogger("intent_router.gate")


class ConfidenceGate:
    """意图决策置信度门控。"""

    def __init__(self):
        self.confidence_threshold = settings.intent_router.INTENT_ROUTER_CONFIDENCE_THRESHOLD
        self.ambiguity_gap = settings.intent_router.INTENT_ROUTER_AMBIGUITY_GAP
        self.tool_registry = get_tool_meta_registry()
        self.tool_entity_threshold = getattr(
            settings.intent_router,
            "INTENT_ROUTER_TOOL_ENTITY_THRESHOLD",
            0.5,
        )

    @staticmethod
    def _top_two_gap(scores: Dict[str, float]) -> float:
        """计算最高分两维度之间的分差。"""
        if len(scores) < 2:
            return 1.0
        sorted_scores = sorted(scores.values(), reverse=True)
        return sorted_scores[0] - sorted_scores[1]

    def _filter_suggested_tools(
        self,
        question: str,
        tools: List[str],
    ) -> List[str]:
        """基于工具元数据过滤 suggested_tools。

        过滤规则：
        - 未知工具（未注册）保留，避免阻塞扩展。
        - 已知工具若实体完整度低于阈值，移除。
        - 已知工具若未命中触发模式，移除。

        Args:
            question: 用户问题（已改写）。
            tools: LLM 推荐的工具列表。

        Returns:
            过滤后的工具列表。
        """
        if not tools:
            return []

        filtered = []
        for name in tools:
            meta = self.tool_registry.get(name)
            if meta is None:
                filtered.append(name)
                continue

            completeness = self.tool_registry.entity_completeness(question, name)
            if completeness < self.tool_entity_threshold:
                logger.debug(
                    f"工具 {name} 实体完整度 {completeness:.2f} 低于阈值，从推荐中移除"
                )
                continue

            matched = self.tool_registry.select_tools(question, [name])
            if not matched:
                logger.debug(f"工具 {name} 未命中触发模式，从推荐中移除")
                continue

            filtered.append(name)

        return filtered

    def apply(
        self,
        draft: IntentDecision,
        question: str,
        has_kb: bool = False,
        use_web_search: bool = False,
        search_mode: str = "simple",
    ) -> IntentDecision:
        """对 LLM 路由草案应用门控规则，返回最终决策。

        Args:
            draft: LLMRouter 输出的决策草案。
            question: 原始用户问题。
            has_kb: 是否已选择知识库。
            use_web_search: 是否开启联网搜索。
            search_mode: 前端传入搜索模式。

        Returns:
            处理后的最终 IntentDecision。
        """
        scores = draft.confidence_scores or {}
        needs_kb_score = scores.get("needs_kb", 0.0)
        needs_web_score = scores.get("needs_web", 0.0)
        needs_realtime_score = scores.get("needs_realtime", 0.0)
        needs_tool_score = scores.get("needs_tool", 0.0)

        # 1. 整体置信度过低 → 直接走 LLM
        if not scores or max(scores.values(), default=0.0) < self.confidence_threshold:
            return IntentDecision(
                primary_mode=PrimaryMode.DIRECT_LLM,
                fallback_strategy=FallbackStrategy.NONE,
                reasoning="LLM 路由置信度过低，降级为直接回答",
                confidence_scores=scores,
                context_rewrite=draft.context_rewrite or question,
                llm_routed=draft.llm_routed,
            )

        # 2. 歧义检测：最高分两意图分差过小 → 触发澄清
        if self._top_two_gap(scores) < self.ambiguity_gap and not draft.needs_clarify:
            draft.needs_clarify = True
            draft.clarify_question = draft.clarify_question or "您的问题可能涉及多个方面，能否再具体说明一下？"

        # 3. 根据环境修正知识库需求
        needs_kb = needs_kb_score >= self.confidence_threshold
        needs_web = needs_web_score >= self.confidence_threshold
        needs_realtime = needs_realtime_score >= self.confidence_threshold
        needs_tool = needs_tool_score >= self.confidence_threshold

        if needs_kb and not has_kb:
            # 没有知识库但 LLM 认为需要 → 尝试用 web search 替代
            needs_kb = False
            needs_web = True
            draft.reasoning = f"{draft.reasoning}；未选择知识库，自动降级为联网搜索"

        if needs_realtime and not use_web_search:
            draft.fallback_strategy = FallbackStrategy.TELL_FAILURE
            draft.reasoning = f"{draft.reasoning}；实时性问题但未开启联网搜索，将说明无法获取实时信息"

        # 4. 根据修正后的布尔值确定主模式（如果 LLM 给的模式与环境冲突）
        primary_mode = draft.primary_mode
        if needs_tool and not needs_kb and not needs_web:
            primary_mode = PrimaryMode.TOOL_FIRST
        elif needs_web and needs_kb:
            primary_mode = PrimaryMode.HYBRID
        elif needs_web and not needs_kb:
            primary_mode = PrimaryMode.WEB_SEARCH
        elif needs_kb and not needs_web:
            primary_mode = PrimaryMode.KB_ONLY
        elif not needs_kb and not needs_web and not needs_tool:
            primary_mode = PrimaryMode.DIRECT_LLM

        # 5. 复杂研究问题且开启高级搜索模式 → Agent
        if primary_mode in (PrimaryMode.WEB_SEARCH, PrimaryMode.HYBRID) and search_mode in ("function_calling", "agent"):
            if any(kw in question for kw in {"对比", "比较", "分析", "总结", "调研", "研究", "优缺点", "区别"}):
                primary_mode = PrimaryMode.AGENT_RESEARCH

        # 6. 设置 fallback_strategy（未设置时根据模式补全）
        fallback = draft.fallback_strategy
        if fallback == FallbackStrategy.NONE:
            if primary_mode in (PrimaryMode.WEB_SEARCH, PrimaryMode.HYBRID, PrimaryMode.AGENT_RESEARCH):
                fallback = FallbackStrategy.TELL_FAILURE if needs_realtime else FallbackStrategy.WEB_SEARCH
            elif primary_mode == PrimaryMode.KB_ONLY:
                fallback = FallbackStrategy.LLM_DIRECT
            elif primary_mode == PrimaryMode.TOOL_FIRST:
                fallback = FallbackStrategy.LLM_DIRECT

        # 7. 确保 context_rewrite 有值
        context_rewrite = draft.context_rewrite or question

        # 8. 基于工具元数据过滤推荐工具
        filtered_tools = self._filter_suggested_tools(context_rewrite, draft.suggested_tools)

        return IntentDecision(
            needs_kb=needs_kb,
            needs_web=needs_web,
            needs_realtime=needs_realtime,
            primary_mode=primary_mode,
            suggested_tools=filtered_tools,
            fallback_strategy=fallback,
            reasoning=draft.reasoning or "LLM 语义路由决策",
            search_pipeline=draft.search_pipeline,
            confidence_scores=scores,
            needs_clarify=draft.needs_clarify,
            clarify_question=draft.clarify_question,
            context_rewrite=context_rewrite,
            llm_routed=draft.llm_routed,
        )
