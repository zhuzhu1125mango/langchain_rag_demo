"""链路追踪采集器。

收集单次请求的完整链路信息，并异步持久化到数据库。
"""

import asyncio
import time
import uuid
from typing import Any, Dict, List, Optional

from src.services.tools.tool_manager import ToolResult


class TraceCollector:
    """单次请求链路追踪采集器。"""

    def __init__(self, trace_id: Optional[str] = None):
        self.trace_id = trace_id or str(uuid.uuid4())
        self.start_time = time.time()
        self.data: Dict[str, Any] = {
            "id": self.trace_id,
        }
        self._finished = False

    def set_basic(
        self,
        question: str,
        resolved_question: Optional[str] = None,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> None:
        """设置基础信息。"""
        self.data["question"] = question
        self.data["resolved_question"] = resolved_question
        self.data["session_id"] = session_id
        self.data["user_id"] = user_id

    def add_stage(self, name: str, status: str = "done", latency_ms: int = 0, detail: Optional[Dict[str, Any]] = None) -> None:
        """记录一个执行阶段的耗时明细（P1-2 可观测性）。"""
        if "stages" not in self.data:
            self.data["stages"] = []
        entry: Dict[str, Any] = {
            "name": name,
            "status": status,
            "latency_ms": latency_ms,
        }
        if detail:
            entry["detail"] = detail
        self.data["stages"].append(entry)

    def set_token_usage(
        self,
        prompt_tokens: Optional[int],
        completion_tokens: Optional[int],
        estimated: bool = False,
    ) -> None:
        """记录 token 用量（模型元数据缺失时可用字符估算并标记 estimated）。"""
        self.data["token_usage"] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "estimated": estimated,
        }

    def set_intent(self, intent_decision) -> None:
        """设置意图决策。"""
        self.data["intent_decision"] = intent_decision.to_dict()
        self.data["primary_mode"] = intent_decision.primary_mode.value

    def add_tool_call(self, result: ToolResult) -> None:
        """添加工具调用结果。"""
        if "tool_calls" not in self.data:
            self.data["tool_calls"] = []
        self.data["tool_calls"].append(result.to_dict())

    def set_tool_calls(self, results: List[ToolResult]) -> None:
        """批量设置工具调用结果。"""
        self.data["tool_calls"] = [r.to_dict() for r in results]

    def set_search_results(self, results: List[Any]) -> None:
        """设置搜索结果。"""
        serialized = []
        for r in results:
            if hasattr(r, "__dict__"):
                serialized.append(r.__dict__)
            elif isinstance(r, dict):
                serialized.append(r)
        self.data["search_results"] = serialized

    def set_kb_results(self, results: List[Any]) -> None:
        """设置知识库检索结果。"""
        serialized = []
        for r in results:
            if hasattr(r, "page_content"):
                serialized.append({
                    "page_content": r.page_content[:500],
                    "metadata": getattr(r, "metadata", {}),
                })
            elif isinstance(r, dict):
                serialized.append(r)
        self.data["kb_results"] = serialized

    def set_fallback(self, triggered: bool, reason: str = "") -> None:
        """设置降级信息。"""
        self.data["fallback_triggered"] = triggered
        self.data["fallback_reason"] = reason

    def set_pollution_detected(self, detected: bool) -> None:
        """设置输出污染检测结果。"""
        self.data["output_pollution_detected"] = detected

    def set_final_answer(self, answer: str) -> None:
        """设置最终答案。"""
        self.data["final_answer"] = answer

    def finish(self) -> Dict[str, Any]:
        """结束采集，计算总耗时。"""
        self.data["total_latency_ms"] = int((time.time() - self.start_time) * 1000)
        self._finished = True
        return self.data

    def to_dict(self) -> Dict[str, Any]:
        """返回当前采集数据。"""
        return self.data.copy()

    async def save_async(self) -> None:
        """异步保存到数据库。"""
        if not self._finished:
            self.finish()

        try:
            from src.database import AsyncSessionLocal
            from src.models.request_trace import RequestTrace

            async with AsyncSessionLocal() as session:
                record = RequestTrace(**self.data)
                session.add(record)
                await session.commit()
        except Exception as e:
            # 保存失败不应影响主流程
            import logging

            logger = logging.getLogger("rag_system")
            logger.warning(f"Trace 持久化失败 [{self.trace_id}]: {e}")

    def save_background(self) -> None:
        """在后台任务中保存（不阻塞主流程）。"""
        try:
            asyncio.create_task(self.save_async())
        except Exception as e:
            import logging

            logger = logging.getLogger("rag_system")
            logger.warning(f"Trace 后台保存任务创建失败 [{self.trace_id}]: {e}")
