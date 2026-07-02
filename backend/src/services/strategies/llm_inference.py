from typing import List, Dict, Optional
from langchain_ollama import ChatOllama
from src.config import settings
from .base import Strategy


class LLMInferenceStrategy(Strategy):
    """
    LLM推理策略
    
    使用LLM进行智能判断，确定问题是否需要使用知识库
    """
    
    def __init__(self):
        self.confidence = 0.0
        self.llm = None
        self.system_related_patterns = [
            "介绍一下",
            "项目功能",
            "系统功能",
            "如何使用",
            "使用方法",
            "帮助",
            "功能说明",
            "什么是",
            "你是谁",
            "你能做什么",
            "你有什么功能",
            "开始使用",
            "快速开始",
            "使用指南"
        ]
    
    async def initialize(self):
        try:
            model_name = settings.model.FAST_LLM_MODEL_NAME or settings.model.OLLAMA_MODEL_NAME
            self.llm = ChatOllama(model=model_name, streaming=False)
        except Exception:
            self.llm = None
    
    def _is_system_question(self, question: str) -> bool:
        """
        判断问题是否是系统相关问题（不需要使用知识库）
        """
        question_lower = question.lower()
        for pattern in self.system_related_patterns:
            if pattern in question_lower:
                return True
        return False
    
    async def should_use_knowledge_base(self, question: str, history: Optional[List[Dict[str, str]]] = None) -> bool:
        """LLM 推理判断是否需要使用知识库。"""
        if self._is_system_question(question):
            self.confidence = 0.95
            return False

        if self.llm is None:
            self.confidence = 0.3
            return False

        template = """
        你是一个智能决策助手，需要判断用户的问题是否需要使用知识库文档来回答。

        问题: {question}

        请严格按照以下规则判断：
        1. 如果问题是问候语、闲聊、自我介绍类问题，可以直接回答的，回答"NO"
        2. 如果问题涉及特定知识、专业内容、需要查阅资料才能回答的问题，回答"YES"
        3. 如果问题是日常对话、情感交流、与知识库无关的问题，回答"NO"
        4. 如果问题是关于系统功能、使用方法、帮助类的问题，回答"NO"
        5. 如果问题是概念解释、定义类问题，且不涉及文档中的特定内容，回答"NO"

        只输出"YES"或"NO"，不要有任何其他内容。
        """

        prompt = template.format(question=question)

        try:
            response = await self.llm.ainvoke(prompt)
            result = response.content.strip().upper()

            # 三态解析：YES 走知识库、NO 走纯 LLM、未识别默认走知识库并降置信度
            if result == "YES":
                self.confidence = 0.85
                return True
            elif result == "NO":
                self.confidence = 0.85
                return False
            else:
                self.confidence = 0.4
                return True
        except Exception:
            self.confidence = 0.3
            return False

    def get_confidence(self) -> float:
        """返回当前置信度。"""
        return self.confidence

    def get_strategy_name(self) -> str:
        """返回 "llm_inference"。"""
        return "llm_inference"

    async def cleanup(self):
        """释放 LLM。"""
        if self.llm is not None:
            del self.llm
            self.llm = None
