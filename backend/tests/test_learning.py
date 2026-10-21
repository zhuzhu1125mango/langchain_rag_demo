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


class TestShortCircuitSampleCollection:
    """LEARN W-B：KB 短路路径的样本补采"""

    async def test_collect_strategy_results_returns_confidences(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        engine = LearningEngine()
        sm = SimpleNamespace(
            should_use_knowledge_base=AsyncMock(return_value=(True, 0.5, {"keyword": 0.9}))
        )
        out = await engine.collect_strategy_results(sm, "q", [])
        assert out == {"keyword": 0.9}

    async def test_collect_strategy_results_swallows_error(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        engine = LearningEngine()
        sm = SimpleNamespace(
            should_use_knowledge_base=AsyncMock(side_effect=RuntimeError("boom"))
        )
        assert await engine.collect_strategy_results(sm, "q") == {}

    async def test_record_chat_execution_reuses_nonempty_results(self):
        """已有策略结果时不补采"""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from src.config import settings

        engine = LearningEngine()
        decision = SimpleNamespace(
            should_use_kb=True, confidence=0.8, strategy_results={"keyword": 0.9}
        )
        sm = SimpleNamespace(
            should_use_knowledge_base=AsyncMock(side_effect=AssertionError("不应补采"))
        )

        with patch.object(engine, "record_execution", new=AsyncMock(return_value="ex-1")) as rec:
            with patch.object(settings.decision, "LEARNING_SAMPLE_COLLECT_BG_ENABLED", True):
                eid = await engine.record_chat_execution(
                    session_id="s1", question="q", decision=decision,
                    used_knowledge_base=True, strategy_manager=sm, history=[]
                )

        assert eid == "ex-1"
        assert rec.await_args.kwargs["strategy_results"] == {"keyword": 0.9}
        sm.should_use_knowledge_base.assert_not_called()

    async def test_record_chat_execution_collects_when_empty_and_enabled(self):
        """结果为空且开关开启时补采"""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from src.config import settings

        engine = LearningEngine()
        decision = SimpleNamespace(should_use_kb=True, confidence=1.0, strategy_results={})
        sm = SimpleNamespace(
            should_use_knowledge_base=AsyncMock(
                return_value=(True, 0.5, {"keyword": 0.9, "context": 0.8})
            )
        )

        with patch.object(engine, "record_execution", new=AsyncMock(return_value="ex-2")) as rec:
            with patch.object(settings.decision, "LEARNING_SAMPLE_COLLECT_BG_ENABLED", True):
                await engine.record_chat_execution(
                    session_id="s1", question="q", decision=decision,
                    used_knowledge_base=True, strategy_manager=sm, history=[]
                )

        assert rec.await_args.kwargs["strategy_results"] == {"keyword": 0.9, "context": 0.8}

    async def test_record_chat_execution_skips_collect_when_disabled(self):
        """开关关闭时保持原行为（空结果直接落库，不补采）"""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from src.config import settings

        engine = LearningEngine()
        decision = SimpleNamespace(should_use_kb=True, confidence=1.0, strategy_results={})
        sm = SimpleNamespace(
            should_use_knowledge_base=AsyncMock(side_effect=AssertionError("不应补采"))
        )

        with patch.object(engine, "record_execution", new=AsyncMock(return_value="ex-3")) as rec:
            with patch.object(settings.decision, "LEARNING_SAMPLE_COLLECT_BG_ENABLED", False):
                await engine.record_chat_execution(
                    session_id="s1", question="q", decision=decision,
                    used_knowledge_base=True, strategy_manager=sm, history=[]
                )

        assert rec.await_args.kwargs["strategy_results"] == {}


class TestExperimentChatWiring:
    """LEARN W-C：A/B 变体请求级覆盖"""

    def test_ab_contextvar_and_rerank_top_k_override(self):
        from src.services.milvus_service import _ab_rerank_top_k
        from src.services.pipeline.state import (
            get_ab_config,
            reset_ab_config,
            set_ab_config,
        )

        assert get_ab_config() is None
        default_top_k = _ab_rerank_top_k()

        token = set_ab_config({"kb_hybrid_rerank_top_k": 9})
        try:
            assert get_ab_config() == {"kb_hybrid_rerank_top_k": 9}
            assert _ab_rerank_top_k() == 9
        finally:
            reset_ab_config(token)

        assert get_ab_config() is None
        assert _ab_rerank_top_k() == default_top_k