from typing import List, Dict, Optional
from .base import Strategy
from .constants import GREETING_KEYWORDS, KNOWLEDGE_KEYWORDS


class ContextStrategy(Strategy):
    """
    上下文分析策略
    
    通过分析历史对话模式来判断当前问题是否需要使用知识库
    """
    
    def __init__(self):
        self.confidence = 0.0
        self.knowledge_trigger_keywords = set(KNOWLEDGE_KEYWORDS)
        self.greeting_keywords = set(GREETING_KEYWORDS) | {"谢谢", "再见", "拜拜"}
    
    def should_use_knowledge_base(self, question: str, history: Optional[List[Dict[str, str]]] = None) -> bool:
        """按历史对话模式评分判断是否需要使用知识库。"""
        question_lower = question.strip().lower()
        context_score = 0.0
        weight_sum = 0.0

        if history and len(history) > 0:
            recent_history = history[-3:]

            for msg in recent_history:
                content = msg.get('content', '').lower()
                role = msg.get('role', '')

                # 统计该条历史中知识触发词与问候词的命中数
                knowledge_count = sum(1 for kw in self.knowledge_trigger_keywords if kw in content)
                greeting_count = sum(1 for kw in self.greeting_keywords if kw in content)

                if role == 'user':
                    # 用户消息权重更高：知识词加分、问候词扣分
                    if knowledge_count > 0:
                        context_score += knowledge_count * 0.25
                        weight_sum += 0.25
                    if greeting_count > 0:
                        context_score -= greeting_count * 0.2
                        weight_sum += 0.2
                elif role == 'assistant':
                    # 助手历史仅作弱参考
                    if knowledge_count > 0:
                        context_score += knowledge_count * 0.15
                        weight_sum += 0.15

        # 当前问题单独评分，权重最大
        knowledge_in_current = sum(1 for kw in self.knowledge_trigger_keywords if kw in question_lower)
        greeting_in_current = sum(1 for kw in self.greeting_keywords if kw in question_lower)

        context_score += knowledge_in_current * 0.3
        weight_sum += 0.3

        if greeting_in_current > 0:
            context_score -= greeting_in_current * 0.25
            weight_sum += 0.25

        # 归一化为 [-1, 1] 区间的得分
        if weight_sum > 0:
            normalized_score = context_score / weight_sum
        else:
            normalized_score = 0.0

        # 正向得分判为需要知识库，负向判为不需要，中间区间按当前问题是否含知识词决定
        if normalized_score > 0.1:
            self.confidence = min(0.7 + normalized_score * 0.3, 0.95)
            return True
        elif normalized_score < -0.1:
            self.confidence = min(0.7 + (1 + normalized_score) * 0.3, 0.95)
            return False
        else:
            self.confidence = 0.5 + normalized_score * 0.5
            return knowledge_in_current > 0

    def get_confidence(self) -> float:
        """返回当前置信度。"""
        return self.confidence

    def get_strategy_name(self) -> str:
        """返回策略名 "context"。"""
        return "context"

    def add_knowledge_keyword(self, keywords: List[str]):
        """动态追加知识触发关键词。"""
        self.knowledge_trigger_keywords.update(keywords)

    def add_greeting_keyword(self, keywords: List[str]):
        """动态追加问候关键词。"""
        self.greeting_keywords.update(keywords)
