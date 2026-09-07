import math
from typing import List, Dict, Optional
from .base import Strategy
from .constants import GREETING_KEYWORDS
from src.services.model_manager import model_manager


class SemanticStrategy(Strategy):
    """
    语义相似度策略

    使用共享 Ollama embeddings（C8：移除 sentence-transformers MiniLM 依赖路径）
    对问题与问候/知识示例做相似度比对，判断问题意图。
    """

    def __init__(self):
        self.confidence = 0.0
        self.greeting_examples = list(GREETING_KEYWORDS) + [
            "早上好", "下午好", "晚上好", "晚安"
        ]
        self.knowledge_examples = [
            "根据文档内容回答", "参考资料回答", "查找相关信息",
            "文档里有什么", "资料里怎么说", "请查阅文档"
        ]
        self.threshold = 0.75
        self.knowledge_threshold = 0.65
        # 示例向量（initialize 时预计算；失败时为 None，策略按低置信度弃权）
        self._greeting_embeddings: Optional[List[List[float]]] = None
        self._knowledge_embeddings: Optional[List[List[float]]] = None

    @staticmethod
    def _cosine(vec_a: List[float], vec_b: List[float]) -> float:
        """余弦相似度。"""
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = math.sqrt(sum(a * a for a in vec_a))
        norm_b = math.sqrt(sum(b * b for b in vec_b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    async def initialize(self):
        """预计算问候/知识示例向量（embedding 失败时策略降级弃权）。"""
        try:
            embeddings = model_manager.get_embeddings()
            self._greeting_embeddings = await embeddings.aembed_documents(self.greeting_examples)
            self._knowledge_embeddings = await embeddings.aembed_documents(self.knowledge_examples)
        except Exception:
            self._greeting_embeddings = None
            self._knowledge_embeddings = None

    async def should_use_knowledge_base(self, question: str, history: Optional[List[Dict[str, str]]] = None) -> bool:
        """按语义相似度判断是否需要使用知识库。"""
        if self._greeting_embeddings is None or self._knowledge_embeddings is None:
            self.confidence = 0.3
            return False

        embeddings = model_manager.get_embeddings()
        question_embedding = await embeddings.aembed_query(question)

        # 与问候示例比对，命中高则判定为闲聊
        max_greeting_score = max(
            self._cosine(question_embedding, vec) for vec in self._greeting_embeddings
        )

        if max_greeting_score >= self.threshold:
            self.confidence = min(max_greeting_score, 0.95)
            return False

        # 与知识示例比对，命中高则判定为需要知识库
        max_knowledge_score = max(
            self._cosine(question_embedding, vec) for vec in self._knowledge_embeddings
        )

        if max_knowledge_score >= self.knowledge_threshold:
            self.confidence = min(max_knowledge_score + 0.2, 0.95)
            return True

        # 两类均未达阈值，默认走知识库，置信度随最大相似度递减
        self.confidence = 0.4 + max(max_greeting_score, max_knowledge_score) * 0.3
        return True

    def get_confidence(self) -> float:
        """返回当前置信度。"""
        return self.confidence

    def get_strategy_name(self) -> str:
        """返回 "semantic"。"""
        return "semantic"

    async def cleanup(self):
        """释放示例向量。"""
        self._greeting_embeddings = None
        self._knowledge_embeddings = None

    def set_threshold(self, threshold: float):
        """设置相似度阈值。"""
        self.threshold = threshold

    async def add_example(self, text: str, is_greeting: bool):
        """追加问候/知识示例并重算对应示例向量。"""
        if is_greeting:
            self.greeting_examples.append(text)
        else:
            self.knowledge_examples.append(text)

        if self._greeting_embeddings is not None and self._knowledge_embeddings is not None:
            try:
                embeddings = model_manager.get_embeddings()
                self._greeting_embeddings = await embeddings.aembed_documents(self.greeting_examples)
                self._knowledge_embeddings = await embeddings.aembed_documents(self.knowledge_examples)
            except Exception:
                self._greeting_embeddings = None
                self._knowledge_embeddings = None
