"""检索策略基类。

定义所有策略必须实现的抽象接口 should_use_knowledge_base 等。
"""

from abc import ABC, abstractmethod
from typing import List, Dict, Optional


class Strategy(ABC):
    """
    策略接口基类
    
    定义所有策略必须实现的接口方法
    """
    
    @abstractmethod
    def should_use_knowledge_base(self, question: str, history: Optional[List[Dict[str, str]]] = None) -> bool:
        """
        判断是否需要使用知识库
        
        Args:
            question: 用户问题
            history: 历史对话列表
            
        Returns:
            bool: True表示需要使用知识库，False表示直接回答
        """
        pass
    
    @abstractmethod
    def get_confidence(self) -> float:
        """
        返回判断置信度
        
        Returns:
            float: 置信度，范围0.0-1.0
        """
        pass
    
    @abstractmethod
    def get_strategy_name(self) -> str:
        """
        返回策略名称
        
        Returns:
            str: 策略名称
        """
        pass
    
    def initialize(self):
        """
        初始化策略（可选）
        
        用于加载模型、配置等初始化操作
        """
        pass
    
    def cleanup(self):
        """
        清理资源（可选）
        
        用于释放模型、关闭连接等操作
        """
        pass
