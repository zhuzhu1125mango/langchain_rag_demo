"""
会话标题生成服务

根据用户第一条消息，使用本地 LLM 自动生成简洁的会话标题。
"""

import logging
import re
from typing import Optional

from langchain_ollama import ChatOllama

from src.config import settings
from src.services.model_manager import model_manager
from src.utils.async_singleton import AsyncSingleton

logger = logging.getLogger(__name__)

DEFAULT_TITLES = {"新会话", "未命名对话", "", "new chat", "new session"}


class TitleGenerator(AsyncSingleton["TitleGenerator"]):
    """基于 LLM 的会话标题生成器"""

    def __init__(self):
        self._llm = None

    async def _get_llm(self) -> Optional[ChatOllama]:
        if self._llm is None:
            try:
                preferred_model = (
                    settings.title_generation.TITLE_GENERATION_MODEL
                    or settings.model.FAST_LLM_MODEL_NAME
                    or settings.model.OLLAMA_MODEL_NAME
                )
                model_name = await model_manager.get_model_for_task(
                    "title_generation",
                    preferred=preferred_model,
                )
                self._llm = ChatOllama(model=model_name, streaming=False)
            except Exception as e:
                logger.warning(f"标题生成 LLM 初始化失败: {e}")
                self._llm = None
        return self._llm

    def _clean_title(self, raw: str) -> str:
        # 去除引号、书名号等多余符号
        cleaned = raw.strip().strip('"').strip("'").strip("《").strip("》")
        # 合并空白字符
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned

    def is_default_title(self, title: Optional[str]) -> bool:
        """判断当前标题是否为默认占位标题"""
        if not title:
            return True
        return title.strip().lower() in DEFAULT_TITLES

    async def generate_title(self, question: str) -> str:
        """
        根据用户首条问题生成会话标题。

        Args:
            question: 用户输入的第一条消息

        Returns:
            str: 生成的标题，失败时返回问题前 N 个字
        """
        if not settings.title_generation.TITLE_GENERATION_ENABLED:
            return self._fallback(question)

        llm = await self._get_llm()
        if llm is None:
            return self._fallback(question)

        prompt = f"""请根据用户的以下问题，生成一个简洁的中文会话标题（不超过{settings.title_generation.TITLE_MAX_LENGTH}个字），用于概括对话主题。
要求：
1. 只输出标题文字，不要输出任何解释、标点或编号
2. 标题尽量简短，保留核心关键词
3. 如果问题是问候语，可以概括为"日常问候"等

用户问题：{question}

标题："""

        try:
            response = await llm.ainvoke(prompt)
            title = self._clean_title(response.content)
            if not title:
                return self._fallback(question)
            max_len = settings.title_generation.TITLE_MAX_LENGTH
            if len(title) > max_len:
                title = title[:max_len]
            logger.info(f"会话标题生成成功: {title}")
            return title
        except Exception as e:
            logger.warning(f"LLM 生成标题失败，使用降级方案: {e}")
            return self._fallback(question)

    def _fallback(self, question: str) -> str:
        """LLM 失败时的降级截断方案。"""
        fallback_len = settings.title_generation.TITLE_FALLBACK_LENGTH
        return question[:fallback_len].strip() or "新会话"


# 保留全局变量以兼容现有调用，但首次访问时需要在异步上下文中完成初始化。
# 推荐在新代码中直接使用 `await TitleGenerator.get_instance()`。
title_generator = None


async def get_title_generator() -> TitleGenerator:
    """获取 TitleGenerator 单例实例。

    用于兼容需要在模块级别访问标题生成器的同步/异步上下文。
    """
    global title_generator
    if title_generator is None:
        title_generator = await TitleGenerator.get_instance()
    return title_generator
