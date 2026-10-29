import logging
from typing import List, Dict, Optional, Tuple

from src.config import settings
from src.utils.error_handling import log_exception

from .strategies.base import Strategy
from .strategies.keyword import KeywordStrategy
from .strategies.semantic import SemanticStrategy
from .strategies.llm_inference import LLMInferenceStrategy
from .strategies.context import ContextStrategy

logger = logging.getLogger("rag_system")


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
    
    def fuse_results(self, results: List[Tuple[str, bool, float]],
                     weights: Optional[Dict[str, float]] = None) -> Tuple[bool, float, Dict[str, float]]:
        """
        融合多个策略的结果

        支持弃权机制：当策略置信度低于弃权阈值时，该策略不参与投票

        Args:
            results: 策略执行结果列表，每个元素为 (策略名, 结果, 置信度)
            weights: 可选的请求级权重覆盖（LEARN W-C A/B），与 self.weights 合并；
                     缺省（None）时用 self.weights，保证默认行为不变

        Returns:
            Tuple[bool, float, Dict[str, float]]: (最终决策, 综合置信度, 各策略置信度)
        """
        effective_weights = {**self.weights, **(weights or {})}
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

            weight = effective_weights.get(name, 0.0)
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
                                  history: Optional[List[Dict[str, str]]] = None,
                                  weights_override: Optional[Dict[str, float]] = None) -> Tuple[bool, float, Dict[str, float]]:
        """
        判断是否需要使用知识库（融合所有策略）
        
        Args:
            question: 用户问题
            history: 历史对话
            weights_override: 可选的请求级权重覆盖（LEARN W-C A/B）
        
        Returns:
            Tuple[bool, float, Dict[str, float]]: (是否使用知识库, 综合置信度, 各策略置信度)
        """
        results = []
        
        for name in self.enabled_strategies:
            if name in self.strategies:
                result, confidence = await self.execute_strategy(name, question, history)
                results.append((name, result, confidence))
        
        return self.fuse_results(results, weights=weights_override)
    
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

    async def load_from_store(self) -> None:
        """从 strategy_configs 回载持久化的权重/启用状态与决策阈值（LEARN W-A）。

        此前 `sample_store.get_strategy_config` 无任何调用方，学到的权重永不被回载
        （三重断链②）。逐策略读取：单条失败仅告警跳过，不影响其余项。阈值单独存放于
        `__decision__` 行的 config.threshold。
        """
        from .sample_store import sample_store

        for name in list(self.strategies.keys()):
            try:
                cfg = await sample_store.get_strategy_config(name)
            except Exception as e:
                # 单策略配置读失败只跳过该策略（用默认开关），但要能定位到策略名，
                # 否则「重启后某策略配置没生效」无从排查
                log_exception(
                    logger,
                    e,
                    action="strategy.reload.config",
                    message="回载策略配置失败，沿用默认开关",
                    strategy=name,
                )
                continue
            if not cfg:
                continue
            weight = cfg.get("weight")
            if weight is not None:
                self.weights[name] = max(0.0, min(1.0, float(weight)))
            enabled = cfg.get("enabled")
            if enabled is not None:
                if enabled:
                    self.enable_strategy(name)
                else:
                    self.disable_strategy(name)

        try:
            decision_cfg = await sample_store.get_strategy_config("__decision__")
        except Exception as e:
            log_exception(
                logger,
                e,
                action="strategy.reload.decision",
                message="回载决策阈值失败，沿用配置默认值",
            )
            decision_cfg = None
        if decision_cfg:
            threshold = (decision_cfg.get("config") or {}).get("threshold")
            if threshold is not None:
                self.set_threshold(float(threshold))


async def create_default_strategy_manager() -> StrategyManager:
    """
    创建默认的策略管理器，包含所有基础策略
    
    Returns:
        StrategyManager: 配置好的策略管理器
    """
    manager = StrategyManager()

    manager.register_strategy(KeywordStrategy())
    # C8：语义策略默认关闭——依赖 sentence-transformers MiniLM，离线环境加载失败时
    # 会永久弃权；关闭后投票权重自然归一到其余策略
    if settings.decision.STRATEGY_SEMANTIC_ENABLED:
        manager.register_strategy(SemanticStrategy())
    manager.register_strategy(LLMInferenceStrategy())
    manager.register_strategy(ContextStrategy())
    
    await manager.initialize_all()
    
    return manager


# LEARN W-A：进程内唯一策略管理器实例。
# 此前 rag_chain._async_init / api.learning / 定时学习任务各自
# create_default_strategy_manager() 新建实例，学习权重调整落在临时对象上、
# 且线上实例从不回载持久化权重（三重断链②）。统一经 get_strategy_manager()
# 获取同一实例，保证学习调整与回载作用于在线决策。
_default_strategy_manager: Optional[StrategyManager] = None


async def get_strategy_manager() -> StrategyManager:
    """返回进程内唯一的策略管理器实例（LEARN W-A）。"""
    global _default_strategy_manager
    if _default_strategy_manager is None:
        _default_strategy_manager = await create_default_strategy_manager()
    return _default_strategy_manager


def reset_strategy_manager() -> None:
    """重置单例（测试用）。"""
    global _default_strategy_manager
    _default_strategy_manager = None
