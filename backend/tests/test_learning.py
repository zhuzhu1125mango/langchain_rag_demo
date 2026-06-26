"""
动态学习系统单元测试
"""

import pytest
from src.services.learning_engine import LearningEngine
from src.services.strategy_manager import StrategyManager


class TestLearningEngine:
    """
    学习引擎测试
    """
    
    def setup_method(self):
        self.engine = LearningEngine()
    
    def test_initial_state(self):
        """测试初始状态"""
        assert self.engine.enabled is True
        assert self.engine.last_learning_time is None
        assert self.engine.learning_interval_hours == 24
    
    def test_enable_disable(self):
        """测试启用/禁用"""
        self.engine.disable()
        assert self.engine.enabled is False
        
        self.engine.enable()
        assert self.engine.enabled is True
    
    def test_set_learning_interval(self):
        """测试设置学习间隔"""
        self.engine.set_learning_interval(12)
        assert self.engine.learning_interval_hours == 12


class TestRuleLearnerIntegration:
    """
    规则学习器集成测试
    """
    
    def test_calculate_strategy_accuracy(self):
        """测试策略准确率计算"""
        samples = [
            {
                "question": "你好",
                "label": 0,
                "strategy_results": {"keyword": 0.1, "context": 0.2}
            },
            {
                "question": "根据文档回答",
                "label": 1,
                "strategy_results": {"keyword": 0.9, "context": 0.8}
            },
            {
                "question": "文档里有什么",
                "label": 1,
                "strategy_results": {"keyword": 0.85, "context": 0.7}
            },
            {
                "question": "再见",
                "label": 0,
                "strategy_results": {"keyword": 0.05, "context": 0.1}
            }
        ]
        
        from src.services.rule_learner import RuleLearner
        learner = RuleLearner()
        
        accuracy = learner.calculate_strategy_accuracy(samples)
        
        assert "keyword" in accuracy
        assert "context" in accuracy
        assert accuracy["keyword"] >= 0.9
        assert accuracy["context"] >= 0.75
    
    def test_train_weight_adjustment(self):
        """测试权重调整训练"""
        samples = [
            {
                "question": "你好",
                "label": 0,
                "strategy_results": {"keyword": 0.1, "context": 0.2}
            },
            {
                "question": "根据文档回答",
                "label": 1,
                "strategy_results": {"keyword": 0.9, "context": 0.6}
            },
            {
                "question": "文档里有什么",
                "label": 1,
                "strategy_results": {"keyword": 0.85, "context": 0.5}
            },
            {
                "question": "再见",
                "label": 0,
                "strategy_results": {"keyword": 0.05, "context": 0.3}
            },
            {
                "question": "嗨",
                "label": 0,
                "strategy_results": {"keyword": 0.08, "context": 0.25}
            },
            {
                "question": "请查阅资料",
                "label": 1,
                "strategy_results": {"keyword": 0.92, "context": 0.55}
            }
        ]
        
        from src.services.rule_learner import RuleLearner
        learner = RuleLearner()
        learner.min_samples_for_training = 5
        
        adjustments = learner.train_weight_adjustment(samples)
        
        assert isinstance(adjustments, dict)
        assert "keyword" in adjustments
        assert adjustments["keyword"] > 0


class TestStrategyManagerIntegration:
    """
    策略管理器集成测试
    """
    
    def test_weight_adjustment(self):
        """测试权重调整"""
        manager = StrategyManager()
        
        initial_weights = manager.weights.copy()
        
        manager.adjust_weights({"keyword": 0.2, "context": -0.1})
        
        assert manager.weights["keyword"] > initial_weights["keyword"]
        assert manager.weights["context"] < initial_weights["context"]
    
    def test_threshold_setting(self):
        """测试阈值设置"""
        manager = StrategyManager()
        
        manager.set_threshold(0.6)
        assert manager.threshold == 0.6
        
        manager.set_threshold(1.5)
        assert manager.threshold == 1.0
        
        manager.set_threshold(-0.1)
        assert manager.threshold == 0.0