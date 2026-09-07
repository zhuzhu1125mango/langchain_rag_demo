"""
策略模块单元测试
"""

import pytest
from src.services.strategies.keyword import KeywordStrategy
from src.services.strategies.context import ContextStrategy
from src.services.strategy_manager import StrategyManager, create_default_strategy_manager


class TestKeywordStrategy:
    """
    关键词匹配策略测试
    """

    def setup_method(self):
        self.strategy = KeywordStrategy()

    async def test_greeting_detection(self):
        """测试问候语检测"""
        greetings = ["你好", "您好", "hi", "hello", "嗨", "在吗", "早上好", "下午好", "晚上好"]
        for greeting in greetings:
            result = await self.strategy.should_use_knowledge_base(greeting)
            assert result is False, f"问候语 '{greeting}' 应该返回 False"

    async def test_knowledge_question_detection(self):
        """测试知识库相关问题检测"""
        knowledge_questions = [
            "请根据文档内容回答",
            "参考资料中的数据是什么",
            "查找相关信息",
            "文档里有什么内容"
        ]
        for question in knowledge_questions:
            result = await self.strategy.should_use_knowledge_base(question)
            assert result is True, f"知识库问题 '{question}' 应该返回 True"

    async def test_confidence_range(self):
        """测试置信度范围"""
        await self.strategy.should_use_knowledge_base("你好")
        confidence = self.strategy.get_confidence()
        assert 0.0 <= confidence <= 1.0, "置信度应在0-1之间"


class TestContextStrategy:
    """
    上下文分析策略测试
    """

    def setup_method(self):
        self.strategy = ContextStrategy()

    async def test_with_history_knowledge_context(self):
        """测试有历史对话上下文（知识库相关）"""
        history = [
            {"role": "user", "content": "请查阅文档"},
            {"role": "assistant", "content": "好的，我将根据文档内容为您解答"},
            {"role": "user", "content": "文档里提到了什么"}
        ]
        result = await self.strategy.should_use_knowledge_base("请详细说明", history)
        assert result is True, "在知识库上下文后提问应该返回 True"

    async def test_with_history_greeting_context(self):
        """测试有历史对话上下文（问候相关）"""
        history = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "您好！有什么可以帮助您的？"},
            {"role": "user", "content": "没什么事"}
        ]
        result = await self.strategy.should_use_knowledge_base("就是打个招呼", history)
        assert result is False, "在问候上下文后提问应该返回 False"


class TestStrategyManager:
    """
    策略管理器测试
    """

    def setup_method(self):
        self.manager = StrategyManager()

    def test_register_strategy(self):
        """测试注册策略"""
        strategy = KeywordStrategy()
        self.manager.register_strategy(strategy)
        assert "keyword" in self.manager.strategies

    def test_strategy_status(self):
        """测试策略状态获取"""
        status = self.manager.get_strategy_status()
        assert isinstance(status, dict)

    def test_weight_adjustment(self):
        """测试权重调整"""
        initial_weight = self.manager.weights.get("keyword", 0.0)
        self.manager.adjust_weights({"keyword": 0.5})
        new_weight = self.manager.weights.get("keyword", 0.0)
        assert new_weight != initial_weight, "权重应该被调整"

    async def test_default_manager_creation(self):
        """测试默认策略管理器创建（C8：语义策略默认关闭，默认注册 3 个策略）"""
        manager = await create_default_strategy_manager()
        assert len(manager.strategies) == 3, "默认策略管理器应该包含3个策略（语义策略默认关闭）"
        assert "semantic" not in manager.strategies

    async def test_semantic_strategy_toggle(self):
        """C8：开启 STRATEGY_SEMANTIC_ENABLED 后默认管理器注册语义策略"""
        from unittest.mock import patch

        from src.config import settings

        with patch.object(settings.decision, "STRATEGY_SEMANTIC_ENABLED", True):
            manager = await create_default_strategy_manager()
        assert len(manager.strategies) == 4
        assert "semantic" in manager.strategies

    def test_decision_threshold(self):
        """测试决策阈值设置"""
        self.manager.set_threshold(0.6)
        assert self.manager.threshold == 0.6
