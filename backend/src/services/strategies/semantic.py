from typing import List, Dict, Optional
from .base import Strategy
from .constants import GREETING_KEYWORDS

# 全局模型缓存，避免每次请求重复加载
_model_cache = {
    'model': None,
    'util': None,
    'greeting_embeddings': None,
    'knowledge_embeddings': None
}


class SemanticStrategy(Strategy):
    """
    语义相似度策略
    
    使用Sentence-BERT进行语义相似度计算，判断问题意图
    """
    
    def __init__(self):
        self.confidence = 0.0
        self.model = None
        self.greeting_examples = list(GREETING_KEYWORDS) + [
            "早上好", "下午好", "晚上好", "晚安"
        ]
        self.knowledge_examples = [
            "根据文档内容回答", "参考资料回答", "查找相关信息",
            "文档里有什么", "资料里怎么说", "请查阅文档"
        ]
        self.threshold = 0.75
        self.knowledge_threshold = 0.65
    
    def initialize(self):
        global _model_cache
        if _model_cache['model'] is not None:
            self.model = _model_cache['model']
            self.util = _model_cache['util']
            self.greeting_embeddings = _model_cache['greeting_embeddings']
            self.knowledge_embeddings = _model_cache['knowledge_embeddings']
            return
        try:
            from sentence_transformers import SentenceTransformer, util
            self.model = SentenceTransformer('all-MiniLM-L6-v2')
            self.util = util
            self.greeting_embeddings = self.model.encode(self.greeting_examples)
            self.knowledge_embeddings = self.model.encode(self.knowledge_examples)
            _model_cache['model'] = self.model
            _model_cache['util'] = self.util
            _model_cache['greeting_embeddings'] = self.greeting_embeddings
            _model_cache['knowledge_embeddings'] = self.knowledge_embeddings
        except ImportError:
            self.model = None
    
    def should_use_knowledge_base(self, question: str, history: Optional[List[Dict[str, str]]] = None) -> bool:
        """按语义相似度判断是否需要使用知识库。"""
        if self.model is None:
            self.confidence = 0.3
            return False

        question_embedding = self.model.encode(question)

        # 与问候示例比对，命中高则判定为闲聊
        greeting_scores = self.util.cos_sim(question_embedding, self.greeting_embeddings)
        max_greeting_score = float(greeting_scores.max())

        if max_greeting_score >= self.threshold:
            self.confidence = min(max_greeting_score, 0.95)
            return False

        # 与知识示例比对，命中高则判定为需要知识库
        knowledge_scores = self.util.cos_sim(question_embedding, self.knowledge_embeddings)
        max_knowledge_score = float(knowledge_scores.max())

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

    def cleanup(self):
        """释放模型资源。"""
        if self.model is not None:
            del self.model
            self.model = None

    def set_threshold(self, threshold: float):
        """设置相似度阈值。"""
        self.threshold = threshold

    def add_example(self, text: str, is_greeting: bool):
        """追加问候/知识示例向量。"""
        if is_greeting:
            self.greeting_examples.append(text)
        else:
            self.knowledge_examples.append(text)
        
        if self.model is not None:
            self.greeting_embeddings = self.model.encode(self.greeting_examples)
            self.knowledge_embeddings = self.model.encode(self.knowledge_examples)
