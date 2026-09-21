"""上下文增强服务。

负责多轮对话的上下文处理：历史对话构建、上下文压缩、指代消解与对话摘要更新。
"""

import logging
from collections import OrderedDict

from src.config import settings
from src.services.intent_router.constants import PRONOUN_PATTERN

logger = logging.getLogger("rag_system")


class ContextEnhancer:
    """
    上下文增强器

    处理多轮对话中的上下文增强，包括指代消解、历史上下文构建、
    上下文压缩和对话摘要管理。
    """

    def __init__(self, llm, max_history_turns: int = 10, max_summary_length: int = 500):
        """
        初始化上下文增强器

        Args:
            llm: LLM 实例
            max_history_turns: 保留的最大历史轮数
            max_summary_length: 摘要最大长度
        """
        self.llm = llm
        self._summaries: "OrderedDict[str, str]" = OrderedDict()
        self._max_summaries = 200
        self.max_history_turns = max_history_turns
        self.max_summary_length = max_summary_length

    def _get_summary(self, session_id: str) -> str:
        """按会话读取摘要并移动到末尾（LRU 访问）。"""
        summary = self._summaries.get(session_id, "")
        if session_id in self._summaries:
            self._summaries.move_to_end(session_id)
        return summary

    def _set_summary(self, session_id: str, text: str) -> None:
        """按会话写入摘要；超过上限时淘汰最久未使用的会话。"""
        self._summaries[session_id] = text
        self._summaries.move_to_end(session_id)
        while len(self._summaries) > self._max_summaries:
            self._summaries.popitem(last=False)

    async def _build_history_context(self, history, session_id: str = ""):
        """
        构建历史对话上下文（支持多轮对话优化）

        Args:
            history: 历史消息列表，每个消息包含role和content
            session_id: 会话ID（摘要按会话隔离，防止跨用户串号）

        Returns:
            str: 格式化的历史对话上下文
        """
        if not history or len(history) == 0:
            return self._get_summary(session_id)

        recent_history = history[-self.max_history_turns:]

        history_lines = []
        for msg in recent_history:
            role = "用户" if msg["role"] == "user" else "助手"
            history_lines.append(f"{role}: {msg['content']}")

        full_context = "\n".join(history_lines)

        if len(full_context) > 2000:
            full_context = await self._compress_context(full_context)

        summary = self._get_summary(session_id)
        if summary:
            return f"对话摘要: {summary}\n\n详细对话:\n{full_context}"

        return full_context

    async def _compress_context(self, context: str) -> str:
        """
        压缩上下文，保留关键信息

        Args:
            context: 原始上下文文本

        Returns:
            str: 压缩后的上下文
        """
        template = """
        请对以下对话历史进行压缩，保留关键信息和核心问题，但不要丢失重要细节：

        对话历史:
        {context}

        请提供一个简洁的摘要（不超过{max_length}字）：
        """
        prompt = template.format(context=context, max_length=self.max_summary_length)

        try:
            response = await self.llm.ainvoke(prompt)
            return response.content.strip()[:self.max_summary_length]
        except Exception as e:
            logger.error(f"上下文压缩失败: {str(e)}", exc_info=True)
            return context[:self.max_summary_length]

    async def _resolve_references(self, question: str, history: list) -> str:
        """
        指代消解：将问题中的代词替换为具体指代内容

        C3 规则前置：无历史或问题不含指代词时直接返回原问题（零 LLM 调用）；
        LLM 消解降级为兜底（CONTEXT_RESOLVE_USE_LLM，默认关闭——
        历史上下文已随提示词携带，规则前置可省去每轮一次的 LLM 调用）。

        Args:
            question: 当前问题
            history: 历史对话列表

        Returns:
            str: 消解后的完整问题
        """
        if not history or len(history) == 0:
            return question

        # 规则前置：问题不含指代词（它/这个/那个/该/上面/刚才…）时无需消解
        if not (question and PRONOUN_PATTERN.search(question)):
            return question

        # LLM 兜底开关关闭：保留原问题（历史对话已在提示词中提供，无错误改写风险）
        if not settings.processing.CONTEXT_RESOLVE_USE_LLM:
            return question

        context = "\n".join([f"{msg['role']}: {msg['content']}" for msg in history[-5:]])

        template = """
        请将以下问题中的代词（如"它"、"这"、"那个"等）替换为具体指代的内容，使问题更加完整清晰。

        对话历史:
        {context}

        当前问题: {question}

        请直接返回消解后的问题，不要添加任何解释。
        """
        prompt = template.format(context=context, question=question)

        try:
            response = await self.llm.ainvoke(prompt)
            result = response.content.strip()
            return result if result else question
        except Exception as e:
            logger.error(f"指代消解失败: {str(e)}", exc_info=True)
            return question

    async def _update_conversation_summary(self, history: list, session_id: str = "") -> str:
        """
        更新对话摘要，用于长对话的上下文管理

        Args:
            history: 历史对话列表
            session_id: 会话ID（摘要按会话隔离，防止跨用户串号）

        Returns:
            str: 更新后的对话摘要
        """
        if not history or len(history) == 0:
            return ""

        recent_history = history[-5:]
        context = "\n".join([f"{msg['role']}: {msg['content']}" for msg in recent_history])
        previous_summary = self._get_summary(session_id)

        template = """
        请对以下对话进行总结，生成一个简洁的对话摘要：

        对话内容:
        {context}

        之前的摘要（如果有）:
        {previous_summary}

        请生成新的对话摘要，保留关键主题和结论（不超过{max_length}字）：
        """
        prompt = template.format(
            context=context,
            previous_summary=previous_summary,
            max_length=self.max_summary_length
        )

        try:
            response = await self.llm.ainvoke(prompt)
            new_summary = response.content.strip()[:self.max_summary_length]
            self._set_summary(session_id, new_summary)
            return new_summary
        except Exception as e:
            logger.error(f"更新对话摘要失败: {str(e)}", exc_info=True)
            return previous_summary

    async def enhance_context(self, question: str, history: list, session_id: str = "") -> dict:
        """
        增强上下文处理，包括指代消解和上下文优化

        Args:
            question: 当前问题
            history: 历史对话列表
            session_id: 会话ID（摘要按会话隔离）

        Returns:
            dict: 包含增强后的问题和上下文信息
        """
        resolved_question = await self._resolve_references(question, history)
        context = await self._build_history_context(history, session_id)

        return {
            "resolved_question": resolved_question,
            "original_question": question,
            "enhanced_context": context,
            "has_resolution": resolved_question != question
        }
