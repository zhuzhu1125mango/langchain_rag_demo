"""LLM 语义意图路由层。

当规则路由无法确定或出现冲突时，调用本地 LLM 进行语义级意图分类，
输出带置信度的结构化决策草案，再交由 ConfidenceGate 做后处理。
"""

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional

from src.config import settings
from src.services.model_manager import model_manager
from src.services.intent_router.conversation_context import (
    ConversationContextBuilder,
    get_default_conversation_context_builder,
)
from src.services.intent_router.models import (
    FallbackStrategy,
    IntentDecision,
    PrimaryMode,
    SearchPipeline,
)

logger = logging.getLogger("intent_router.llm")


# 意图路由专用 prompt，要求模型输出结构化 JSON
_INTENT_ROUTER_PROMPT = """你是一名意图分类专家。请根据用户问题（结合历史对话）判断最合适的处理模式。

可选主模式（primary_mode）：
- direct_llm：日常闲聊、问候、自我介绍、无需检索即可回答的问题
- kb_only：问题明确需要查阅文档/资料/制度/流程
- tool_first：需要时间、天气、金价、汇率、计算等专用工具
- web_search：强时效性问题，需要联网搜索实时信息
- agent_research：复杂多步研究问题（对比、分析、调研、总结多个来源）
- hybrid：同时需要知识库和联网搜索

可用工具（suggested_tools）：get_current_time, weather_query, gold_price, exchange_rate, calculator, web_search, fetch_webpage

输出要求：
1. 只输出一个 JSON 对象，不要 markdown 代码块，不要解释
2. JSON 字段如下：
{
  "needs_kb": 0.0~1.0,
  "needs_web": 0.0~1.0,
  "needs_realtime": 0.0~1.0,
  "needs_tool": 0.0~1.0,
  "primary_mode": "direct_llm|kb_only|tool_first|web_search|agent_research|hybrid",
  "suggested_tools": ["tool_name"],
  "search_pipeline": "fast_path|full_path",
  "needs_clarify": false,
  "clarify_question": "",
  "context_rewrite": "结合历史补全后的标准问题",
  "reasoning": "简短说明理由"
}

判断规则：
- 问候语、闲聊 → direct_llm，所有 needs_* 给低分
- 时间/日期 → tool_first，suggested_tools=["get_current_time"]
- 含城市名的天气 → tool_first，suggested_tools=["weather_query"]
- 金价/银价/贵金属价格 → tool_first，suggested_tools=["gold_price"]
- 汇率/兑换 → tool_first，suggested_tools=["exchange_rate"]
- 数学计算 → tool_first，suggested_tools=["calculator"]
- "文档/资料/制度/流程/第几条" → kb_only，needs_kb 高分
- "今天/最新/现在/实时/新闻/股价"等 → needs_realtime 高分，primary_mode 视是否有知识库选择 web_search 或 hybrid
- 复杂研究意图（对比/分析/调研/总结）→ agent_research 或 hybrid
- 模糊问法如"帮我查一下这个"、"它怎么样" → needs_clarify=true，并给出澄清问题

当前问题：{question}
历史对话：
{history}

请只输出 JSON："""


class LLMRouter:
    """基于本地 LLM 的语义意图路由器。"""

    _instance: Optional["LLMRouter"] = None
    _lock = asyncio.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._llm = None
            cls._instance._model_name = None
            cls._instance._context_builder = None
        return cls._instance

    def _get_context_builder(self) -> ConversationContextBuilder:
        """获取或初始化对话上下文构建器。"""
        if self._context_builder is None:
            self._context_builder = get_default_conversation_context_builder()
        return self._context_builder

    async def _get_llm(self) -> Optional[Any]:
        """获取或初始化 LLM 实例（B1 工厂：think=False），失败时返回 None。"""
        preferred_model = settings.intent_router.INTENT_ROUTER_LLM_MODEL or None
        # 通过模型管理器检查可用性并自动降级
        current_model = await model_manager.get_model_for_task(
            "intent_router",
            preferred=preferred_model,
        )
        if self._llm is None or self._model_name != current_model:
            try:
                logger.info(f"初始化意图路由 LLM: {current_model}")
                # B1 工厂：统一走 fast 任务角色；JSON 分类小任务关闭思考链
                self._llm = await model_manager.get_chat_llm(
                    "intent_router",
                    think=False,
                    timeout=settings.intent_router.INTENT_ROUTER_LLM_TIMEOUT,
                )
                self._model_name = current_model
            except Exception as e:
                logger.warning(f"意图路由 LLM 初始化失败: {e}")
                self._llm = None
        return self._llm

    @staticmethod
    def _extract_json(text: str) -> Optional[str]:
        """从模型输出中提取 JSON 块，兼容 markdown 代码块。"""
        # 尝试直接解析
        text = text.strip()
        # 去除可能的 markdown 代码块标记
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        # 尝试匹配最外层 {}
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            return match.group(0)
        return text if text.startswith("{") else None

    @staticmethod
    def _safe_float(value, default: float = 0.0) -> float:
        """安全地将值转为 0~1 之间的浮点数。"""
        try:
            f = float(value)
        except (TypeError, ValueError):
            return default
        if f < 0:
            return 0.0
        if f > 1:
            return 1.0
        return f

    def _parse_llm_output(self, content: str) -> IntentDecision:
        """解析 LLM 输出为 IntentDecision 草案。"""
        json_str = self._extract_json(content)
        if not json_str:
            raise ValueError("未从 LLM 输出中提取到 JSON")

        data = json.loads(json_str)

        confidence_scores = {
            "needs_kb": self._safe_float(data.get("needs_kb"), 0.0),
            "needs_web": self._safe_float(data.get("needs_web"), 0.0),
            "needs_realtime": self._safe_float(data.get("needs_realtime"), 0.0),
            "needs_tool": self._safe_float(data.get("needs_tool"), 0.0),
        }

        primary_mode_str = data.get("primary_mode", "direct_llm")
        try:
            primary_mode = PrimaryMode(primary_mode_str)
        except ValueError:
            primary_mode = PrimaryMode.DIRECT_LLM

        search_pipeline_str = data.get("search_pipeline", "fast_path")
        try:
            search_pipeline = SearchPipeline(search_pipeline_str)
        except ValueError:
            search_pipeline = SearchPipeline.FAST_PATH

        suggested_tools = data.get("suggested_tools") or []
        if not isinstance(suggested_tools, list):
            suggested_tools = []

        context_rewrite = data.get("context_rewrite", "")
        if not isinstance(context_rewrite, str):
            context_rewrite = ""

        needs_clarify = bool(data.get("needs_clarify", False))
        clarify_question = data.get("clarify_question", "")
        if not isinstance(clarify_question, str):
            clarify_question = ""

        return IntentDecision(
            confidence_scores=confidence_scores,
            primary_mode=primary_mode,
            suggested_tools=suggested_tools,
            search_pipeline=search_pipeline,
            needs_clarify=needs_clarify,
            clarify_question=clarify_question,
            context_rewrite=context_rewrite,
            reasoning=data.get("reasoning", ""),
            llm_routed=True,
        )

    async def route(
        self,
        question: str,
        history: Optional[List[Dict]] = None,
    ) -> IntentDecision:
        """调用 LLM 进行语义意图分类。

        流程：
        1. 使用 ConversationContextBuilder 做历史压缩与指代消解。
        2. 将消解后的问题与压缩历史输入 LLM。
        3. 解析 LLM 输出的结构化决策。

        Args:
            question: 用户问题。
            history: 对话历史（可选）。

        Returns:
            IntentDecision 草案。

        Raises:
            RuntimeError: LLM 不可用或调用失败时抛出，由调用方降级。
        """
        llm = await self._get_llm()
        if llm is None:
            raise RuntimeError("意图路由 LLM 未初始化")

        context = self._get_context_builder().build(question, history)
        resolved_question = context.resolved_question or question
        compressed_history = context.compressed_history or "无"

        prompt = _INTENT_ROUTER_PROMPT.format(
            question=resolved_question,
            history=compressed_history,
        )

        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(llm.invoke, prompt),
                timeout=settings.intent_router.INTENT_ROUTER_LLM_TIMEOUT,
            )
            content = response.content if hasattr(response, "content") else str(response)
            decision = self._parse_llm_output(content)
            # 若 LLM 未做改写，使用 ConversationContextBuilder 的消解结果
            if not decision.context_rewrite:
                decision.context_rewrite = resolved_question
            return decision
        except asyncio.TimeoutError:
            logger.warning("意图路由 LLM 调用超时")
            raise RuntimeError("LLM 路由超时")
        except Exception as e:
            logger.warning(f"意图路由 LLM 调用失败: {e}")
            raise RuntimeError(f"LLM 路由失败: {e}")
