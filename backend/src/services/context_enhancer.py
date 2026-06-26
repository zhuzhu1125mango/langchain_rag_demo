"""上下文增强服务。

负责多轮对话的上下文处理：历史对话构建、上下文压缩、指代消解与对话摘要更新。
"""

import logging

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
        self.conversation_summary = ""
        self.max_history_turns = max_history_turns
        self.max_summary_length = max_summary_length

    async def _build_history_context(self, history):
        """
        构建历史对话上下文（支持多轮对话优化）

        Args:
            history: 历史消息列表，每个消息包含role和content

        Returns:
            str: 格式化的历史对话上下文
        """
        if not history or len(history) == 0:
            return self.conversation_summary

        recent_history = history[-self.max_history_turns:]

        history_lines = []
        for msg in recent_history:
            role = "用户" if msg["role"] == "user" else "助手"
            history_lines.append(f"{role}: {msg['content']}")

        full_context = "\n".join(history_lines)

        if len(full_context) > 2000:
            full_context = await self._compress_context(full_context)

        if self.conversation_summary:
            return f"对话摘要: {self.conversation_summary}\n\n详细对话:\n{full_context}"

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

        Args:
            question: 当前问题
            history: 历史对话列表

        Returns:
            str: 消解后的完整问题
        """
        if not history or len(history) == 0:
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

    async def _update_conversation_summary(self, history: list) -> str:
        """
        更新对话摘要，用于长对话的上下文管理

        Args:
            history: 历史对话列表

        Returns:
            str: 更新后的对话摘要
        """
        if not history or len(history) == 0:
            return ""

        recent_history = history[-5:]
        context = "\n".join([f"{msg['role']}: {msg['content']}" for msg in recent_history])

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
            previous_summary=self.conversation_summary,
            max_length=self.max_summary_length
        )

        try:
            response = await self.llm.ainvoke(prompt)
            self.conversation_summary = response.content.strip()[:self.max_summary_length]
            return self.conversation_summary
        except Exception as e:
            logger.error(f"更新对话摘要失败: {str(e)}", exc_info=True)
            return self.conversation_summary

    async def enhance_context(self, question: str, history: list) -> dict:
        """
        增强上下文处理，包括指代消解和上下文优化

        Args:
            question: 当前问题
            history: 历史对话列表

        Returns:
            dict: 包含增强后的问题和上下文信息
        """
        resolved_question = await self._resolve_references(question, history)
        context = await self._build_history_context(history)

        return {
            "resolved_question": resolved_question,
            "original_question": question,
            "enhanced_context": context,
            "has_resolution": resolved_question != question
        }
