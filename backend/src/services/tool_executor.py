"""工具执行器。

根据 IntentRouter 的决策，调用一个或多个工具，收集结果。
支持单次调用和迭代式多步调用。
"""

from typing import Dict, List, Optional

from src.services.intent_router import FallbackStrategy, IntentDecision
from src.services.tools.tool_manager import ToolManager, ToolResult


class ToolExecutor:
    """工具执行器，负责按决策调用工具并聚合结果。"""

    def __init__(self, tool_manager: Optional[ToolManager] = None):
        self.tool_manager = tool_manager or ToolManager()
        # 如果未传入 ToolManager，自动发现所有工具
        if not tool_manager:
            self.tool_manager.discover_tools()

    async def execute(
        self,
        decision: IntentDecision,
        question: str,
        history_context: str = "",
    ) -> List[ToolResult]:
        """根据意图决策执行工具。

        目前主要支持单次调用。后续可扩展为基于上一步结果的多轮迭代。

        Args:
            decision: 意图决策。
            question: 用户原始问题。
            history_context: 历史上下文。

        Returns:
            ToolResult 列表。
        """
        if not decision.suggested_tools:
            return []

        calls = []
        for tool_name in decision.suggested_tools:
            arguments = self._build_arguments(tool_name, question, history_context)
            calls.append({"tool_name": tool_name, "arguments": arguments})

        results = await self.tool_manager.execute_parallel(calls)
        return results

    def _build_arguments(
        self, tool_name: str, question: str, history_context: str
    ) -> Dict[str, any]:
        """根据工具类型构造调用参数。"""
        if tool_name == "weather_query":
            return {"question": question}
        if tool_name == "gold_price":
            return {"question": question}
        if tool_name == "exchange_rate":
            return {"question": question}
        if tool_name == "calculator":
            return {"expression": question}
        if tool_name == "get_current_time":
            return {}
        if tool_name == "web_search":
            return {"query": question, "reason": "回答用户问题"}
        if tool_name == "fetch_webpage":
            return {"url": question}
        return {"question": question, "history_context": history_context}

    async def execute_with_fallback(
        self,
        decision: IntentDecision,
        question: str,
        history_context: str = "",
    ) -> List[ToolResult]:
        """执行工具并在失败时按降级策略重试。"""
        results = await self.execute(decision, question, history_context)

        # 如果主工具全部失败，根据 fallback_strategy 尝试降级
        all_failed = all(not r.success for r in results)
        if all_failed and decision.fallback_strategy == FallbackStrategy.WEB_SEARCH:
            fallback_result = await self.tool_manager.execute(
                "web_search", query=question, reason="工具失败后降级搜索"
            )
            if fallback_result.success:
                return [fallback_result]

        return results
