"""意图路由数据模型。

定义路由决策所需的核心枚举与数据类，保持与旧版 IntentDecision 的字段兼容。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class PrimaryMode(str, Enum):
    """问题主处理模式。"""

    DIRECT_LLM = "direct_llm"
    KB_ONLY = "kb_only"
    TOOL_FIRST = "tool_first"
    WEB_SEARCH = "web_search"
    AGENT_RESEARCH = "agent_research"
    HYBRID = "hybrid"


class FallbackStrategy(str, Enum):
    """失败时的降级策略。"""

    NONE = "none"
    WEB_SEARCH = "web_search"
    LLM_DIRECT = "llm_direct"
    TELL_FAILURE = "tell_failure"


class SearchPipeline(str, Enum):
    """联网搜索分级流水线。

    - FAST_PATH：80% 简单问题，规则 Rewriter + 单次 LLM 生成，~6s。
    - FULL_PATH：20% 复杂问题，LLM Rewriter + 交叉验证 + embedding 引用补全，~12s。
    """

    FAST_PATH = "fast_path"
    FULL_PATH = "full_path"


@dataclass
class IntentDecision:
    """意图决策结果。

    在原有字段基础上新增置信度、澄清、上下文补全等扩展字段，新增字段均有默认值，
    保证旧代码消费时不会受影响。
    """

    needs_kb: bool = False
    needs_web: bool = False
    needs_realtime: bool = False
    primary_mode: PrimaryMode = PrimaryMode.DIRECT_LLM
    suggested_tools: List[str] = field(default_factory=list)
    fallback_strategy: FallbackStrategy = FallbackStrategy.NONE
    reasoning: str = ""
    search_pipeline: SearchPipeline = SearchPipeline.FAST_PATH

    # Phase 2 新增字段
    confidence_scores: Dict[str, float] = field(default_factory=dict)
    needs_clarify: bool = False
    clarify_question: str = ""
    context_rewrite: str = ""
    rule_hit: bool = False
    llm_routed: bool = False

    def to_dict(self) -> Dict[str, any]:
        """将决策结果序列化为字典，便于日志与 API 返回。"""
        return {
            "needs_kb": self.needs_kb,
            "needs_web": self.needs_web,
            "needs_realtime": self.needs_realtime,
            "primary_mode": self.primary_mode.value,
            "suggested_tools": self.suggested_tools,
            "fallback_strategy": self.fallback_strategy.value,
            "reasoning": self.reasoning,
            "search_pipeline": self.search_pipeline.value,
            "confidence_scores": self.confidence_scores,
            "needs_clarify": self.needs_clarify,
            "clarify_question": self.clarify_question,
            "context_rewrite": self.context_rewrite,
            "rule_hit": self.rule_hit,
            "llm_routed": self.llm_routed,
        }
