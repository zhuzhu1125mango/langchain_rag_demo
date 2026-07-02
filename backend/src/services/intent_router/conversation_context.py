"""对话上下文组件。

为意图路由层提供轻量级历史压缩与指代补全，不替代 RAG 主链路的 ContextEnhancer。
"""

import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from src.config import settings

logger = logging.getLogger("intent_router.context")


# 常见中文代词与指示词
_PRONOUNS = {"它", "这", "那", "这个", "那个", "其", "此", "该", "上面", "刚才", "之前"}

# 用于从历史消息中提取主题实体的简单模式
_TOPIC_STOPWORDS = {
    "是", "的", "了", "在", "和", "与", "或", "有", "没有", "多少", "什么",
    "怎么", "为什么", "如何", "请", "帮我", "给我", "查询", "查一下", "告诉我",
    "今天", "现在", "当前", "最新", "最近", "实时",
    "你好", "您好", "嗨", "哈喽", "hello", "hi", "hey", "在吗", "再见", "拜拜",
}


@dataclass
class ConversationContext:
    """对话上下文处理结果。

    Attributes:
        resolved_question: 指代消解/补全后的问题。
        compressed_history: 压缩后的历史对话文本。
        original_question: 原始问题。
        has_resolution: 是否发生过改写。
    """

    resolved_question: str = ""
    compressed_history: str = ""
    original_question: str = ""
    has_resolution: bool = False


class ConversationContextBuilder:
    """构建意图路由所需的对话上下文。

    功能：
    - 截断过长历史，必要时进行简单压缩
    - 对含代词/省略的问题，基于最近一轮主题做轻量级补全
    """

    def __init__(
        self,
        max_history_turns: int = 5,
        max_history_chars: int = 1200,
        llm=None,
    ):
        self.max_history_turns = max_history_turns
        self.max_history_chars = max_history_chars
        self.llm = llm

    def build(
        self,
        question: str,
        history: Optional[List[Dict]] = None,
    ) -> ConversationContext:
        """构建上下文。

        Args:
            question: 当前用户问题。
            history: 历史对话列表，每个元素包含 role 和 content。

        Returns:
            ConversationContext: 包含消解后问题和压缩历史。
        """
        question = (question or "").strip()
        history = history or []

        resolved_question = self._resolve_pronouns(question, history)
        compressed_history = self._compress_history(history)

        return ConversationContext(
            resolved_question=resolved_question,
            compressed_history=compressed_history,
            original_question=question,
            has_resolution=resolved_question != question,
        )

    def _compress_history(self, history: List[Dict]) -> str:
        """压缩历史对话。

        策略：
        1. 只保留最近 N 轮。
        2. 若总长度超过阈值，保留最近 2 轮完整内容，更早的轮次只保留用户问题。
        3. 仍超长则截断尾部。
        """
        if not history:
            return ""

        recent = history[-self.max_history_turns:] if len(history) > self.max_history_turns else history
        lines = []
        for turn in recent:
            role = turn.get("role", "user")
            content = turn.get("content", "")
            label = "用户" if role == "user" else "助手"
            lines.append(f"{label}: {content}")

        full = "\n".join(lines)
        if len(full) <= self.max_history_chars:
            return full

        # 保留最近 2 轮完整内容，更早的只保留用户问题
        if len(recent) > 2:
            older = recent[:-2]
            recent_two = recent[-2:]
            older_lines = []
            for turn in older:
                if turn.get("role") == "user":
                    older_lines.append(f"用户: {turn.get('content', '')}")
            full = "\n".join(older_lines + [
                f"{'用户' if t.get('role') == 'user' else '助手'}: {t.get('content', '')}"
                for t in recent_two
            ])

        if len(full) > self.max_history_chars:
            full = full[: self.max_history_chars] + "..."

        return full

    def _resolve_pronouns(self, question: str, history: List[Dict]) -> str:
        """轻量级指代消解。

        仅当问题包含代词/指示词时，尝试从最近的用户问题中提取主题实体并补全。
        若无法提取可靠实体，返回原问题。
        """
        if not history:
            return question

        # 1. 判断是否含代词
        found_pronoun = any(p in question for p in _PRONOUNS)
        if not found_pronoun:
            return question

        # 2. 从最近的用户消息中提取候选主题
        topic = self._extract_recent_topic(history)
        if not topic:
            return question

        # 3. 替换首个出现的代词为主题
        for p in sorted(_PRONOUNS, key=len, reverse=True):
            if p in question:
                resolved = question.replace(p, topic, 1)
                logger.debug(f"指代消解: '{question}' -> '{resolved}' (topic={topic})")
                return resolved

        return question

    def _extract_recent_topic(self, history: List[Dict]) -> str:
        """从历史中提取最近的用户关注主题。

        策略：
        - 优先取最近一条用户消息。
        - 若最近用户消息含疑问词，尝试提取名词短语作为主题。
        - 若最近用户消息只是简单确认，再往前找一条。
        """
        for turn in reversed(history):
            if turn.get("role") != "user":
                continue
            content = turn.get("content", "").strip()
            if not content:
                continue

            topic = self._extract_topic_entity(content)
            if topic:
                return topic

        # 兜底：从最近助手回答中提取关键名词（简单做法：取第一个较长片段）
        for turn in reversed(history):
            if turn.get("role") == "assistant":
                content = turn.get("content", "").strip()
                topic = self._extract_topic_entity(content)
                if topic:
                    return topic

        return ""

    def _extract_topic_entity(self, text: str) -> str:
        """从文本中提取主题实体（简单启发式）。

        优先匹配：
        - 价格/天气/汇率等工具相关的复合名词
        - 引号内的内容
        - 较长的连续名词/名称
        """
        text = text.strip()
        if not text or len(text) < 2:
            return ""

        # 若整句较短且不含停用词，直接作为主题
        if len(text) <= 10 and not any(sw in text for sw in _TOPIC_STOPWORDS):
            return text

        # 优先匹配引号内容
        quoted = re.findall(r'["""'']([^"""'']+)["""'']', text)
        if quoted:
            candidate = quoted[-1].strip()
            if len(candidate) >= 2:
                return candidate

        # 优先匹配常见工具相关主题模式
        tool_patterns = [
            r"([\u4e00-\u9fa5]{2,6}(?:价格|金价|银价|汇率|天气|气温|股票|股价))",
            r"([A-Z]{3}).*?(?:汇率|兑换|换算)",
            r"([\u4e00-\u9fa5]+(?:市|县|区|镇))(?:的?天气)",
        ]
        for pattern in tool_patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1)

        # 去除句首疑问词和请求词，取剩余部分作为主题
        cleaned = re.sub(r"^(?:请问|请|帮我|给我|查询|查一下|告诉我|我想知道|我想了解)\s*", "", text)
        cleaned = re.sub(r"[\s?？。！!]", "", cleaned)

        # 去掉尾部疑问词
        cleaned = re.sub(r"(多少|什么|怎么|吗|呢|吧|行不行|可以吗)$", "", cleaned)

        if len(cleaned) >= 2 and cleaned not in _TOPIC_STOPWORDS:
            return cleaned

        return ""


def get_default_conversation_context_builder(llm=None) -> ConversationContextBuilder:
    """获取默认的 ConversationContextBuilder 实例。"""
    max_turns = getattr(
        settings.intent_router,
        "INTENT_ROUTER_MAX_HISTORY_TURNS",
        5,
    )
    return ConversationContextBuilder(
        max_history_turns=max_turns,
        llm=llm,
    )
