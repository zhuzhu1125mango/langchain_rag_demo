"""
学习引擎 - LearningEngine

整合样本存储和规则学习器，实现完整的动态学习功能
"""

from typing import List, Dict, Optional, Any
import asyncio
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from src.database import async_session
from .sample_store import sample_store
from .rule_learner import rule_learner


class LearningEngine:
    """
    学习引擎类

    负责协调样本收集、规则学习和策略更新
    支持将配置持久化到数据库
    """

    def __init__(self):
        self.last_learning_time = None
        self.learning_interval_hours = 24
        self.enabled = True

    async def load_config(self):
        """从数据库加载配置（每次都重新加载，确保与数据库同步）"""
        from src.models.learning_engine_config import LearningEngineConfig
        async with async_session() as session:
            result = await session.execute(
                select(LearningEngineConfig).order_by(LearningEngineConfig.created_at.desc()).limit(1)
            )
            config = result.scalar_one_or_none()
            if config:
                self.enabled = config.enabled
                self.learning_interval_hours = config.learning_interval_hours
                self.last_learning_time = config.last_learning_time

    async def save_config(self):
        """保存配置到数据库"""
        from src.models.learning_engine_config import LearningEngineConfig
        async with async_session() as session:
            result = await session.execute(
                select(LearningEngineConfig).order_by(LearningEngineConfig.created_at.desc()).limit(1)
            )
            config = result.scalar_one_or_none()
            if config:
                config.enabled = self.enabled
                config.learning_interval_hours = self.learning_interval_hours
                config.last_learning_time = self.last_learning_time
                # SQLAlchemy async 中 onupdate 可能不自动生效，显式更新 updated_at
                config.updated_at = datetime.now(timezone.utc)
                # config 已从当前会话查询获得，处于 persistent 状态，直接 commit 即可
            else:
                config = LearningEngineConfig(
                    enabled=self.enabled,
                    learning_interval_hours=self.learning_interval_hours,
                    last_learning_time=self.last_learning_time
                )
                session.add(config)
            await session.commit()
    
    async def record_execution(self, session_id: str, question: str, 
                              final_decision: bool, final_confidence: float,
                              strategy_results: Dict[str, float], 
                              used_knowledge_base: bool) -> str:
        """
        记录策略执行
        
        Args:
            session_id: 会话ID
            question: 用户问题
            final_decision: 最终决策
            final_confidence: 综合置信度
            strategy_results: 各策略执行结果
            used_knowledge_base: 是否使用知识库
            
        Returns:
            str: 执行记录ID
        """
        return await sample_store.save_execution(
            session_id=session_id,
            question=question,
            final_decision=final_decision,
            final_confidence=final_confidence,
            strategy_results=strategy_results,
            used_knowledge_base=used_knowledge_base
        )
    
    async def record_feedback(self, execution_id: str, feedback_score: float, 
                             reason: Optional[str] = None):
        """
        记录用户反馈
        
        Args:
            execution_id: 执行记录ID
            feedback_score: 反馈分数（-1到1）
            reason: 反馈原因（可选）
        """
        execution = await sample_store.get_execution(execution_id)
        
        if execution:
            strategy_results = execution.get("strategy_results", {})
            
            for strategy_name in strategy_results.keys():
                await sample_store.save_feedback(
                    execution_id=execution_id,
                    strategy_name=strategy_name,
                    feedback_score=feedback_score,
                    reason=reason
                )
    
    async def trigger_learning(self, strategy_manager) -> Dict[str, Any]:
        """
        触发学习流程

        Args:
            strategy_manager: 策略管理器实例

        Returns:
            Dict[str, Any]: 学习结果
        """
        if not self.enabled:
            return {"status": "skipped", "reason": "学习引擎已禁用"}

        results = {}

        results["weight_update"] = await rule_learner.learn_and_update(strategy_manager)

        results["threshold_update"] = await rule_learner.update_threshold_based_on_feedback(strategy_manager)

        self.last_learning_time = datetime.now(timezone.utc)
        await self.save_config()

        return results
    
    async def check_and_trigger_learning(self, strategy_manager) -> Dict[str, Any]:
        """
        检查是否需要触发学习并执行
        
        Args:
            strategy_manager: 策略管理器实例
            
        Returns:
            Dict[str, Any]: 学习结果
        """
        if not self.enabled:
            return {"status": "skipped", "reason": "学习引擎已禁用"}
        
        if self.last_learning_time is None:
            return await self.trigger_learning(strategy_manager)
        
        time_since_last_learning = datetime.now(timezone.utc) - self.last_learning_time
        
        if time_since_last_learning >= timedelta(hours=self.learning_interval_hours):
            return await self.trigger_learning(strategy_manager)
        
        return {
            "status": "skipped",
            "reason": f"学习间隔未到，下次学习时间: {(self.last_learning_time + timedelta(hours=self.learning_interval_hours)).isoformat()}"
        }
    
    async def get_learning_stats(self) -> Dict[str, Any]:
        """
        获取学习统计信息

        Returns:
            Dict[str, Any]: 统计信息
        """
        await self.load_config()

        execution_stats = await sample_store.get_execution_stats()

        strategy_stats = {}
        for strategy_name in ["keyword", "semantic", "llm_inference", "context"]:
            stats = await sample_store.get_strategy_feedback_stats(strategy_name)
            strategy_stats[strategy_name] = stats

        return {
            "execution_stats": execution_stats,
            "strategy_feedback_stats": strategy_stats,
            "last_learning_time": self.last_learning_time.isoformat() if self.last_learning_time else None,
            "learning_interval_hours": self.learning_interval_hours,
            "enabled": self.enabled
        }
    
    async def get_misclassification_analysis(self, limit: int = 50) -> Dict[str, Any]:
        """
        获取误分类分析报告
        
        Args:
            limit: 返回数量限制
            
        Returns:
            Dict[str, Any]: 分析报告
        """
        misclassified = await rule_learner.analyze_misclassifications(limit)
        suggested_keywords = await rule_learner.suggest_new_keywords(limit)
        
        return {
            "misclassified_cases": misclassified,
            "suggested_keywords": suggested_keywords,
            "analysis_time": datetime.now(timezone.utc).isoformat()
        }
    
    async def schedule_periodic_learning(self, strategy_manager, interval_hours: int = 24):
        """
        定时调度学习任务
        
        Args:
            strategy_manager: 策略管理器实例
            interval_hours: 学习间隔（小时）
        """
        self.learning_interval_hours = interval_hours
        
        while True:
            await self.check_and_trigger_learning(strategy_manager)
            await asyncio.sleep(interval_hours * 3600)
    
    def set_learning_interval(self, hours: int):
        """
        设置学习间隔
        
        Args:
            hours: 间隔小时数
        """
        self.learning_interval_hours = hours
    
    def enable(self):
        """启用学习引擎"""
        self.enabled = True
    
    def disable(self):
        """禁用学习引擎"""
        self.enabled = False


learning_engine = LearningEngine()
