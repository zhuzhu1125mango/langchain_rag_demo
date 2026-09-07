import re
from dataclasses import dataclass
from typing import Dict
from enum import Enum

from src.config import settings


# 常见问候/日常对话模式，命中后跳过知识库检索
_GREETING_PATTERNS = [
    r"^(你好|您好|嗨|哈喽|hello|hi|hey|在吗|在?|有人吗|早上好|中午好|晚上好|晚安|再见|拜拜|bye)\s*[.!?！？]*\s*$",
    r"^(请?问?)?\s*(你是谁|你能做什么|介绍一下自己|自我介绍一下|你好啊|您好啊)\s*[.!?！？]*\s*$",
]
_GREETING_RE = re.compile("|".join(_GREETING_PATTERNS), re.IGNORECASE)


class QAMode(Enum):
    """问答模式枚举（pure_llm/pure_kb/hybrid/web_search/function_calling/agent_search）。"""

    PURE_LLM = "pure_llm"
    PURE_KB = "pure_kb"
    HYBRID_INTELLIGENT = "hybrid"
    WEB_SEARCH = "web_search"
    HYBRID_SEARCH = "hybrid_search"
    FUNCTION_CALLING = "function_calling"
    AGENT_SEARCH = "agent_search"


@dataclass
class DecisionResult:
    """决策结果数据类（含 should_use_kb/confidence/mode/strategy_results/reasoning）。"""

    should_use_kb: bool
    confidence: float
    mode: QAMode
    strategy_results: Dict[str, float]
    reasoning: str


class DecisionPipeline:
    """决策管道：根据用户问题、知识库选择及功能开关决定问答模式。"""

    def __init__(self, strategy_manager):
        self.strategy_manager = strategy_manager

    @staticmethod
    def is_greeting(question: str) -> bool:
        """判断是否为问候/日常对话，命中时跳过知识库直接由 LLM 回答。"""
        if not question:
            return False
        return bool(_GREETING_RE.match(question.strip()))

    async def decide(self, question, kb_ids=None, history=None, force_mode=None, use_web_search=False, search_mode="simple"):
        """决定当前问题应使用的问答模式。

        决策优先级：问候 > Function Calling / Agent > 联网搜索 > 强制模式 > 策略管理器。
        """
        # 问候/日常对话直接走 LLM，不检索知识库
        if self.is_greeting(question):
            return DecisionResult(
                should_use_kb=False,
                confidence=1.0,
                mode=QAMode.PURE_LLM,
                strategy_results={},
                reasoning="问候/日常对话，直接由 LLM 回答"
            )

        # Phase 3: 工具调用 / ReAct Agent 模式（受功能开关控制）
        if search_mode == "function_calling":
            if settings.search.SEARCH_ENABLE_FUNCTION_CALLING:
                return DecisionResult(
                    should_use_kb=bool(kb_ids and len(kb_ids) > 0),
                    confidence=1.0,
                    mode=QAMode.FUNCTION_CALLING,
                    strategy_results={},
                    reasoning="用户选择 Function Calling 工具调用模式"
                )
            # 功能未开启时降级为普通联网搜索或纯 LLM
            use_web_search = True

        if search_mode == "agent":
            if settings.search.SEARCH_ENABLE_REACT:
                return DecisionResult(
                    should_use_kb=bool(kb_ids and len(kb_ids) > 0),
                    confidence=1.0,
                    mode=QAMode.AGENT_SEARCH,
                    strategy_results={},
                    reasoning="用户选择 ReAct Agent 多步推理搜索模式"
                )
            use_web_search = True

        # 联网搜索模式：有知识库则混合检索，否则纯联网
        if use_web_search:
            if kb_ids and len(kb_ids) > 0:
                return DecisionResult(
                    should_use_kb=True,
                    confidence=1.0,
                    mode=QAMode.HYBRID_SEARCH,
                    strategy_results={},
                    reasoning="用户选择联网搜索+知识库混合模式"
                )
            else:
                return DecisionResult(
                    should_use_kb=False,
                    confidence=1.0,
                    mode=QAMode.WEB_SEARCH,
                    strategy_results={},
                    reasoning="用户选择纯联网搜索模式"
                )

        # 用户未选择知识库，直接走纯 LLM 模式
        if not kb_ids or len(kb_ids) == 0:
            return DecisionResult(
                should_use_kb=False,
                confidence=1.0,
                mode=QAMode.PURE_LLM,
                strategy_results={},
                reasoning="用户未选择知识库，使用纯LLM模式"
            )

        # 强制模式：用户显式指定问答模式，跳过策略判断
        if force_mode == QAMode.PURE_LLM:
            return DecisionResult(
                should_use_kb=False,
                confidence=1.0,
                mode=QAMode.PURE_LLM,
                strategy_results={},
                reasoning="强制使用纯LLM模式"
            )

        if force_mode == QAMode.PURE_KB:
            return DecisionResult(
                should_use_kb=True,
                confidence=1.0,
                mode=QAMode.PURE_KB,
                strategy_results={},
                reasoning="强制使用知识库模式"
            )

        # C5：用户显式勾选知识库且未强制指定模式时，跳过多策略投票直接检索。
        # 回答模板已保证"参考信息不足时结合模型自身知识回答"，跳过投票不改变回答质量约束，
        # 仅省去首字前的 LLM 投票耗时；策略投票结果与用户显式意图相悖时反而造成困惑。
        if kb_ids and len(kb_ids) > 0 and not settings.decision.DECISION_VOTE_WHEN_KB_SELECTED:
            return DecisionResult(
                should_use_kb=True,
                confidence=1.0,
                mode=QAMode.HYBRID_INTELLIGENT,
                strategy_results={},
                reasoning="用户显式选择知识库，跳过策略投票直接检索"
            )

        # 兜末级：交给策略管理器多策略投票决策
        use_kb, confidence, strategy_results = await self.strategy_manager.should_use_knowledge_base(question, history)
        reasoning = "混合智能模式判断：" + ("需要使用知识库" if use_kb else "直接回答更合适")

        return DecisionResult(
            should_use_kb=use_kb,
            confidence=confidence,
            mode=QAMode.HYBRID_INTELLIGENT,
            strategy_results=strategy_results,
            reasoning=reasoning
        )
