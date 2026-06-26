"""
样本存储服务 - SampleStore

负责收集、存储和管理策略执行样本数据，为动态学习提供数据支持
"""

from typing import List, Dict, Optional, Any
from datetime import datetime, timedelta
from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession
from src.database import async_session
from src.models.strategy import StrategyExecution, StrategyFeedback, StrategyConfig


class SampleStore:
    """
    样本存储服务类
    
    提供策略执行样本的存储、查询和统计功能
    """
    
    async def save_execution(self, session_id: str, question: str, 
                            final_decision: bool, final_confidence: float,
                            strategy_results: Dict[str, float], 
                            used_knowledge_base: bool) -> str:
        """
        保存策略执行记录
        
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
        async with async_session() as session:
            execution = StrategyExecution(
                session_id=session_id,
                question=question,
                final_decision=final_decision,
                final_confidence=final_confidence,
                strategy_results=strategy_results,
                used_knowledge_base=used_knowledge_base
            )
            session.add(execution)
            await session.commit()
            await session.refresh(execution)
            return str(execution.id)
    
    async def save_feedback(self, execution_id: str, strategy_name: str,
                           feedback_score: float, reason: Optional[str] = None):
        """
        保存策略反馈记录
        
        Args:
            execution_id: 执行记录ID
            strategy_name: 策略名称
            feedback_score: 反馈分数（-1到1）
            reason: 反馈原因（可选）
        """
        async with async_session() as session:
            feedback = StrategyFeedback(
                execution_id=execution_id,
                strategy_name=strategy_name,
                feedback_score=feedback_score,
                reason=reason
            )
            session.add(feedback)
            await session.commit()
    
    async def get_execution(self, execution_id: str) -> Optional[Dict[str, Any]]:
        """
        获取单个执行记录
        
        Args:
            execution_id: 执行记录ID
            
        Returns:
            Dict[str, Any] or None: 执行记录信息
        """
        async with async_session() as session:
            result = await session.execute(
                select(StrategyExecution).where(StrategyExecution.id == execution_id)
            )
            execution = result.scalar_one_or_none()
            if execution:
                return {
                    "id": str(execution.id),
                    "session_id": str(execution.session_id),
                    "question": execution.question,
                    "final_decision": execution.final_decision,
                    "final_confidence": execution.final_confidence,
                    "strategy_results": execution.strategy_results,
                    "used_knowledge_base": execution.used_knowledge_base,
                    "created_at": execution.created_at.isoformat()
                }
            return None
    
    async def get_executions_by_session(self, session_id: str) -> List[Dict[str, Any]]:
        """
        获取指定会话的所有执行记录
        
        Args:
            session_id: 会话ID
            
        Returns:
            List[Dict[str, Any]]: 执行记录列表
        """
        async with async_session() as session:
            result = await session.execute(
                select(StrategyExecution).where(StrategyExecution.session_id == session_id)
            )
            executions = result.scalars().all()
            return [
                {
                    "id": str(e.id),
                    "question": e.question,
                    "final_decision": e.final_decision,
                    "final_confidence": e.final_confidence,
                    "strategy_results": e.strategy_results,
                    "created_at": e.created_at.isoformat()
                }
                for e in executions
            ]
    
    async def get_recent_executions(self, limit: int = 100) -> List[Dict[str, Any]]:
        """
        获取最近的执行记录
        
        Args:
            limit: 返回数量限制
            
        Returns:
            List[Dict[str, Any]]: 执行记录列表
        """
        async with async_session() as session:
            result = await session.execute(
                select(StrategyExecution).order_by(StrategyExecution.created_at.desc()).limit(limit)
            )
            executions = result.scalars().all()
            return [
                {
                    "id": str(e.id),
                    "question": e.question,
                    "final_decision": e.final_decision,
                    "final_confidence": e.final_confidence,
                    "strategy_results": e.strategy_results,
                    "created_at": e.created_at.isoformat()
                }
                for e in executions
            ]
    
    async def get_executions_with_feedback(self, limit: int = 100) -> List[Dict[str, Any]]:
        """
        获取最近的执行记录及其反馈（单次查询，避免N+1问题）
        
        Args:
            limit: 返回数量限制
            
        Returns:
            List[Dict[str, Any]]: 执行记录列表，包含反馈信息
        """
        async with async_session() as session:
            result = await session.execute(
                select(StrategyExecution, StrategyFeedback)
                .outerjoin(StrategyFeedback, StrategyExecution.id == StrategyFeedback.execution_id)
                .order_by(StrategyExecution.created_at.desc())
                .limit(limit)
            )
            
            execution_map = {}
            
            for execution, feedback in result.all():
                exec_id = str(execution.id)
                
                if exec_id not in execution_map:
                    execution_map[exec_id] = {
                        "id": exec_id,
                        "question": execution.question,
                        "final_decision": execution.final_decision,
                        "final_confidence": execution.final_confidence,
                        "strategy_results": execution.strategy_results,
                        "feedbacks": []
                    }
                
                if feedback:
                    execution_map[exec_id]["feedbacks"].append({
                        "strategy_name": feedback.strategy_name,
                        "feedback_score": feedback.feedback_score,
                        "reason": feedback.reason
                    })
            
            return list(execution_map.values())
    
    async def get_feedback_for_execution(self, execution_id: str) -> List[Dict[str, Any]]:
        """
        获取指定执行记录的反馈
        
        Args:
            execution_id: 执行记录ID
            
        Returns:
            List[Dict[str, Any]]: 反馈列表
        """
        async with async_session() as session:
            result = await session.execute(
                select(StrategyFeedback).where(StrategyFeedback.execution_id == execution_id)
            )
            feedbacks = result.scalars().all()
            return [
                {
                    "id": str(f.id),
                    "strategy_name": f.strategy_name,
                    "feedback_score": f.feedback_score,
                    "reason": f.reason,
                    "created_at": f.created_at.isoformat()
                }
                for f in feedbacks
            ]
    
    async def get_strategy_feedback_stats(self, strategy_name: str) -> Dict[str, float]:
        """
        获取策略的反馈统计
        
        Args:
            strategy_name: 策略名称
            
        Returns:
            Dict[str, float]: 统计信息
        """
        async with async_session() as session:
            result = await session.execute(
                select(StrategyFeedback).where(StrategyFeedback.strategy_name == strategy_name)
            )
            feedbacks = result.scalars().all()
            
            if not feedbacks:
                return {"count": 0, "average_score": 0.0, "positive_count": 0, "negative_count": 0}
            
            scores = [f.feedback_score for f in feedbacks]
            positive_count = sum(1 for s in scores if s > 0)
            negative_count = sum(1 for s in scores if s < 0)
            
            return {
                "count": len(feedbacks),
                "average_score": sum(scores) / len(scores),
                "positive_count": positive_count,
                "negative_count": negative_count
            }
    
    async def get_execution_stats(self) -> Dict[str, Any]:
        """
        获取执行记录统计
        
        Returns:
            Dict[str, Any]: 统计信息
        """
        async with async_session() as session:
            result = await session.execute(select(StrategyExecution))
            executions = result.scalars().all()
            
            if not executions:
                return {
                    "total_count": 0,
                    "knowledge_base_used_count": 0,
                    "average_confidence": 0.0,
                    "daily_counts": {}
                }
            
            knowledge_base_count = sum(1 for e in executions if e.used_knowledge_base)
            avg_confidence = sum(e.final_confidence for e in executions) / len(executions)
            
            daily_counts = {}
            for e in executions:
                date_str = e.created_at.date().isoformat()
                daily_counts[date_str] = daily_counts.get(date_str, 0) + 1
            
            return {
                "total_count": len(executions),
                "knowledge_base_used_count": knowledge_base_count,
                "average_confidence": avg_confidence,
                "daily_counts": daily_counts
            }
    
    async def update_strategy_config(self, strategy_name: str, config: Dict[str, Any]):
        """
        更新策略配置
        
        Args:
            strategy_name: 策略名称
            config: 配置字典
        """
        async with async_session() as session:
            result = await session.execute(
                select(StrategyConfig).where(StrategyConfig.strategy_name == strategy_name)
            )
            config_obj = result.scalar_one_or_none()
            
            if config_obj:
                for key, value in config.items():
                    setattr(config_obj, key, value)
            else:
                config_obj = StrategyConfig(strategy_name=strategy_name, **config)
                session.add(config_obj)
            
            await session.commit()
    
    async def get_strategy_config(self, strategy_name: str) -> Optional[Dict[str, Any]]:
        """
        获取策略配置
        
        Args:
            strategy_name: 策略名称
            
        Returns:
            Dict[str, Any] or None: 配置信息
        """
        async with async_session() as session:
            result = await session.execute(
                select(StrategyConfig).where(StrategyConfig.strategy_name == strategy_name)
            )
            config = result.scalar_one_or_none()
            if config:
                return {
                    "strategy_name": config.strategy_name,
                    "weight": config.weight,
                    "enabled": config.enabled,
                    "config": config.config,
                    "updated_at": config.updated_at.isoformat() if config.updated_at else None
                }
            return None
    
    async def delete_old_executions(self, days_to_keep: int = 30):
        """
        删除旧的执行记录
        
        Args:
            days_to_keep: 保留天数
        """
        cutoff_date = datetime.now() - timedelta(days=days_to_keep)
        async with async_session() as session:
            await session.execute(
                delete(StrategyExecution).where(StrategyExecution.created_at < cutoff_date)
            )
            await session.commit()


sample_store = SampleStore()
