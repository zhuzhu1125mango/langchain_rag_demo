"""推理/搜索过程收集与序列化模块。

定义 RAG 链路中各阶段 reasoning 步骤的标准结构，提供 SSE 事件负载构造、
步骤去重与合并能力，供 rag_chain.py 与 chat.py 统一使用。
"""

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


class ReasoningStep:
    """单个 reasoning 步骤。

    Attributes:
        id: 步骤唯一标识，用于前端 diff/更新。
        step: 步骤类型，如 intent_routing / web_search / kb_retrieve 等。
        status: 状态：running / done / failed。
        title: 前端展示标题。
        content: 前端展示内容。
        timestamp: 创建时间戳（秒）。
        duration_ms: 耗时（毫秒），可选。
        metadata: 额外结构化数据，如来源数、搜索关键词等。
    """

    def __init__(
        self,
        step: str,
        status: str,
        title: str,
        content: str = "",
        duration_ms: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.id = str(uuid.uuid4())
        self.step = step
        self.status = status
        self.title = title
        self.content = content
        self.timestamp = time.time()
        self.duration_ms = duration_ms
        self.metadata = metadata or {}

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典，供 SSE / 数据库存储使用。"""
        return {
            "id": self.id,
            "step": self.step,
            "status": self.status,
            "title": self.title,
            "content": self.content,
            "timestamp": self.timestamp,
            "duration_ms": self.duration_ms,
            "metadata": self.metadata,
        }

    def to_sse_payload(self) -> str:
        """构造 SSE 事件 JSON 字符串。"""
        return json.dumps({"type": "reasoning", **self.to_dict()})


class ReasoningCollector:
    """收集一次问答链路中的 reasoning 步骤。

    支持按 step 去重：同一阶段多次更新时，用新步骤替换旧步骤，
    避免前端时间线出现重复条目。
    """

    def __init__(self):
        self._steps: List[ReasoningStep] = []
        self._index_by_step: Dict[str, int] = {}
        self._start_times: Dict[str, float] = {}

    def start(self, step: str, title: str, content: str = "", metadata: Optional[Dict[str, Any]] = None) -> ReasoningStep:
        """记录某阶段开始。"""
        self._start_times[step] = time.time()
        return self.upsert(
            step=step,
            status="running",
            title=title,
            content=content,
            metadata=metadata,
        )

    def finish(
        self,
        step: str,
        title: Optional[str] = None,
        content: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ReasoningStep:
        """记录某阶段完成，自动计算耗时。"""
        start_time = self._start_times.pop(step, None)
        duration_ms = None
        if start_time is not None:
            duration_ms = int((time.time() - start_time) * 1000)
        return self.upsert(
            step=step,
            status="done",
            title=title or self._get_existing_title(step),
            content=content,
            duration_ms=duration_ms,
            metadata=metadata,
        )

    def fail(
        self,
        step: str,
        title: Optional[str] = None,
        content: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ReasoningStep:
        """记录某阶段失败。"""
        start_time = self._start_times.pop(step, None)
        duration_ms = None
        if start_time is not None:
            duration_ms = int((time.time() - start_time) * 1000)
        return self.upsert(
            step=step,
            status="failed",
            title=title or self._get_existing_title(step),
            content=content,
            duration_ms=duration_ms,
            metadata=metadata,
        )

    def upsert(
        self,
        step: str,
        status: str,
        title: str,
        content: str = "",
        duration_ms: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ReasoningStep:
        """新增或替换同一阶段的步骤。"""
        step_obj = ReasoningStep(
            step=step,
            status=status,
            title=title,
            content=content,
            duration_ms=duration_ms,
            metadata=metadata or {},
        )
        if step in self._index_by_step:
            idx = self._index_by_step[step]
            self._steps[idx] = step_obj
        else:
            self._steps.append(step_obj)
            self._index_by_step[step] = len(self._steps) - 1
        return step_obj

    def _get_existing_title(self, step: str) -> str:
        """获取已有步骤标题，用于 finish/fail 时保持标题一致。"""
        if step in self._index_by_step:
            return self._steps[self._index_by_step[step]].title
        return step

    def to_list(self) -> List[Dict[str, Any]]:
        """导出为字典列表，供持久化使用。"""
        return [s.to_dict() for s in self._steps]

    def __len__(self) -> int:
        return len(self._steps)


# 标准步骤类型常量
REASONING_STEP_INTENT = "intent_routing"


def dedupe_steps(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 step 去重事件列表：同一阶段保留最后一次出现的事件。

    与 ReasoningCollector 的 upsert 语义一致（新事件替换旧事件），
    用于 SSE end 负载与持久化前清理逐事件累积产生的 running/done 重复条目。
    """
    deduped: Dict[str, Dict[str, Any]] = {}
    for s in steps:
        deduped[s.get("step", "")] = s
    return list(deduped.values())


REASONING_STEP_CONTEXT_REWRITE = "context_rewrite"
REASONING_STEP_WEB_SEARCH = "web_search"
REASONING_STEP_KB_RETRIEVE = "kb_retrieve"
REASONING_STEP_TOOL_EXECUTE = "tool_execute"
REASONING_STEP_ANSWER_GENERATE = "answer_generate"
REASONING_STEP_CACHE_HIT = "cache_hit"
REASONING_STEP_FALLBACK = "fallback"
