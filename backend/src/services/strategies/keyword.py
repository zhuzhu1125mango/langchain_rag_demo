import re
from typing import List, Dict, Optional, Set
from .base import Strategy
from .constants import KNOWLEDGE_KEYWORDS


class KeywordStrategy(Strategy):
    """
    关键词匹配策略
    
    通过精确匹配和正则表达式来判断是否需要使用知识库
    """
    
    def __init__(self):
        self.confidence = 0.0
        self.exact_matches: Set[str] = {
            "你好", "您好", "hi", "hello", "嗨", "在吗",
            "你是谁", "介绍一下", "你能做什么", "谢谢", "再见", "拜拜",
            "早上好", "下午好", "晚上好", "晚安", "早安",
            "很高兴认识你", "初次见面", "久仰大名"
        }
        
        self.regex_patterns = [
            r"^(嗨|嘿|哈喽|Hi|Hello|Hey)\s*[!。，]?$",
            r"^(在吗|在不在|有人吗|有人在线吗)\s*[?？]?$",
            r"^(谢谢|感谢|多谢|辛苦了)\s*[!。]?$",
            r"^(再见|拜拜|告辞|下次见|回见)\s*[!。]?$",
            r"^(你是谁|你叫什么|你的名字|介绍一下你自己)\s*[?？]?$",
            r"^(你能做什么|你会什么|你的功能|有什么用)\s*[?？]?$",
            r"^(早上好|上午好|中午好|下午好|晚上好|晚安|早安)\s*[!。]?$",
            r"^(好的|知道了|明白了|收到|OK|ok)\s*[!。]?$",
            r"^(是的|对|没错|正确|准确)\s*[!。]?$",
            r"^(不是|不对|错误|不正确)\s*[!。]?$"
        ]
        
        self.knowledge_keywords: Set[str] = {
            "根据", "依据", "参考", "查阅", "查找",
            "文档", "资料", "文件", "报告", "数据",
            "内容", "信息", "知识", "了解", "知道",
            "什么是", "什么叫", "定义", "解释", "说明",
            "如何", "怎么", "怎样", "方法", "步骤",
            "为什么", "原因", "理由", "原理", "机制"
        }
    
    async def should_use_knowledge_base(self, question: str, history: Optional[List[Dict[str, str]]] = None) -> bool:
        """精确匹配+正则判断是否需要使用知识库。"""
        question_lower = question.strip().lower()

        for pattern in self.regex_patterns:
            if re.match(pattern, question_lower):
                self.confidence = 0.95
                return False

        for exact in self.exact_matches:
            if exact.lower() == question_lower or exact.lower() in question_lower:
                self.confidence = 0.90
                return False

        keyword_count = sum(1 for kw in self.knowledge_keywords if kw in question_lower)
        if keyword_count >= 2:
            self.confidence = min(0.8 + keyword_count * 0.05, 0.95)
            return True

        if keyword_count == 1:
            self.confidence = 0.6
            return True

        self.confidence = 0.3
        return False

    def get_confidence(self) -> float:
        """返回当前置信度。"""
        return self.confidence

    def get_strategy_name(self) -> str:
        """返回 "keyword"。"""
        return "keyword"

    def add_exact_match(self, keywords: List[str]):
        """动态追加精确匹配词。"""
        self.exact_matches.update(keywords)

    def add_regex_pattern(self, patterns: List[str]):
        """动态追加正则模式。"""
        self.regex_patterns.extend(patterns)

    def add_knowledge_keyword(self, keywords: List[str]):
        """动态追加知识关键词。"""
        self.knowledge_keywords.update(keywords)
