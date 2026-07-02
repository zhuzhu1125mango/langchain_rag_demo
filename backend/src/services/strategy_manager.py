from typing import List, Dict, Optional, Tuple
from .strategies.base import Strategy
from .strategies.keyword import KeywordStrategy
from .strategies.semantic import SemanticStrategy
from .strategies.llm_inference import LLMInferenceStrategy
from .strategies.context import ContextStrategy


class StrategyManager:
    """
    策略管理器
    
    负责策略的注册、选择、权重融合和执行
    支持"弃权"机制：低置信度策略不参与投票
    """
    
    def __init__(self):
        self.strategies: Dict[str, Strategy] = {}
        self.weights: Dict[str, float] = {
            "keyword": 0.25,
            "semantic": 0.25,
            "llm_inference": 0.30,
            "context": 0.20
        }
        self.threshold = 0.5
        self.enabled_strategies: List[str] = []
        self.abstention_threshold = 0.35
    
    def register_strategy(self, strategy: Strategy):
        """
        注册策略
        
        Args:
            strategy: 策略实例
        """
        name = strategy.get_strategy_name()
        self.strategies[name] = strategy
        if name not in self.enabled_strategies:
            self.enabled_strategies.append(name)
    
    def unregister_strategy(self, name: str):
        """
        注销策略
        
        Args:
            name: 策略名称
        """
        if name in self.strategies:
            del self.strategies[name]
        if name in self.enabled_strategies:
            self.enabled_strategies.remove(name)
    
    def set_weight(self, strategy_name: str, weight: float):
        """
        设置策略权重
        
        Args:
            strategy_name: 策略名称
            weight: 权重值（0.0-1.0）
        """
        if strategy_name in self.strategies:
            self.weights[strategy_name] = max(0.0, min(1.0, weight))
    
    def set_threshold(self, threshold: float):
        """
        设置决策阈值
        
        Args:
            threshold: 阈值（0.0-1.0）
        """
        self.threshold = max(0.0, min(1.0, threshold))
    
    def enable_strategy(self, name: str):
        """
        启用策略
        
        Args:
            name: 策略名称
        """
        if name in self.strategies and name not in self.enabled_strategies:
            self.enabled_strategies.append(name)
    
    def disable_strategy(self, name: str):
        """
        禁用策略
        
        Args:
            name: 策略名称
        """
        if name in self.enabled_strategies:
            self.enabled_strategies.remove(name)
    
    async def initialize_all(self):
        """
        初始化所有已注册的策略
        """
        for strategy in self.strategies.values():
            await strategy.initialize()
    
    async def cleanup_all(self):
        """
        清理所有策略资源
        """
        for strategy in self.strategies.values():
            await strategy.cleanup()
    
    async def execute_strategy(self, strategy_name: str, question: str, 
                        history: Optional[List[Dict[str, str]]] = None) -> Tuple[bool, float]:
        """
        执行单个策略
        
        Args:
            strategy_name: 策略名称
            question: 用户问题
            history: 历史对话
        
        Returns:
            Tuple[bool, float]: (是否使用知识库, 置信度)
        """
        if strategy_name not in self.strategies:
            return False, 0.0
        
        strategy = self.strategies[strategy_name]
        result = await strategy.should_use_knowledge_base(question, history)
        confidence = strategy.get_confidence()
        
        return result, confidence
    
    def set_abstention_threshold(self, threshold: float):
        """
        设置弃权阈值
        
        Args:
            threshold: 阈值（0.0-1.0），置信度低于此值的策略将弃权不参与投票
        """
        self.abstention_threshold = max(0.0, min(1.0, threshold))
    
    def fuse_results(self, results: List[Tuple[str, bool, float]]) -> Tuple[bool, float, Dict[str, float]]:
        """
        融合多个策略的结果
        
        支持弃权机制：当策略置信度低于弃权阈值时，该策略不参与投票
        
        Args:
            results: 策略执行结果列表，每个元素为 (策略名, 结果, 置信度)
        
        Returns:
            Tuple[bool, float, Dict[str, float]]: (最终决策, 综合置信度, 各策略置信度)
        """
        total_weight = 0.0
        weighted_score = 0.0
        strategy_confidences: Dict[str, float] = {}
        abstained_strategies: List[str] = []
        
        for name, result, confidence in results:
            if name not in self.enabled_strategies:
                continue

            strategy_confidences[name] = confidence

            # 弃权机制：置信度低于阈值的策略不参与投票，避免噪声策略拉偏结果
            if confidence < self.abstention_threshold:
                abstained_strategies.append(name)
                continue

            weight = self.weights.get(name, 0.0)
            total_weight += weight

            # 投"是"按置信度计入正分，投"否"按 (1-置信度) 计入
            if result:
                weighted_score += weight * confidence
            else:
                weighted_score += weight * (1 - confidence)

        # 所有策略均弃权，默认不使用知识库
        if total_weight == 0.0:
            return False, 0.0, strategy_confidences

        # 归一化得分并与决策阈值比较；置信度取决策方向上的得分
        final_score = weighted_score / total_weight
        final_decision = final_score >= self.threshold
        final_confidence = final_score if final_decision else 1 - final_score
        
        return final_decision, min(final_confidence, 0.99), strategy_confidences
    
    async def should_use_knowledge_base(self, question: str, 
                                  history: Optional[List[Dict[str, str]]] = None) -> Tuple[bool, float, Dict[str, float]]:
        """
        判断是否需要使用知识库（融合所有策略）
        
        Args:
            question: 用户问题
            history: 历史对话
        
        Returns:
            Tuple[bool, float, Dict[str, float]]: (是否使用知识库, 综合置信度, 各策略置信度)
        """
        results = []
        
        for name in self.enabled_strategies:
            if name in self.strategies:
                result, confidence = await self.execute_strategy(name, question, history)
                results.append((name, result, confidence))
        
        return self.fuse_results(results)
    
    def get_strategy_status(self) -> Dict[str, Dict[str, float]]:
        """
        获取所有策略的状态信息
        
        Returns:
            Dict[str, Dict[str, float]]: 策略状态字典
        """
        status = {}
        for name, strategy in self.strategies.items():
            status[name] = {
                "weight": self.weights.get(name, 0.0),
                "enabled": name in self.enabled_strategies
            }
        status["_config"] = {
            "decision_threshold": self.threshold,
            "abstention_threshold": self.abstention_threshold
        }
        return status
    
    def adjust_weights(self, feedback: Dict[str, float]):
        """
        根据反馈动态调整策略权重
        
        Args:
            feedback: 策略反馈字典，key为策略名，value为反馈分数（-1到1）
        """
        for name, score in feedback.items():
            if name in self.weights:
                adjustment = score * 0.1
                new_weight = self.weights[name] + adjustment
                self.weights[name] = max(0.05, min(0.95, new_weight))
        
        total = sum(self.weights.values())
        if total > 0:
            for name in self.weights:
                self.weights[name] /= total


async def create_default_strategy_manager() -> StrategyManager:
    """
    创建默认的策略管理器，包含所有基础策略
    
    Returns:
        StrategyManager: 配置好的策略管理器
    """
    manager = StrategyManager()
    
    manager.register_strategy(KeywordStrategy())
    manager.register_strategy(SemanticStrategy())
    manager.register_strategy(LLMInferenceStrategy())
    manager.register_strategy(ContextStrategy())
    
    await manager.initialize_all()
    
    return manager
