"""
策略配置模型 - StrategyConfig

定义策略配置数据模型，用于持久化策略权重和配置参数
"""

from sqlalchemy import Column, String, Float, Boolean, DateTime, JSON, UUID, ForeignKey
from sqlalchemy.sql import func
import uuid
from src.database import Base


class StrategyConfig(Base):
    """
    策略配置模型类
    
    存储各策略的权重配置和启用状态
    """
    
    __tablename__ = "strategy_configs"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    strategy_name = Column(String, unique=True, nullable=False)  # 策略名称
    weight = Column(Float, default=0.25)                        # 策略权重
    enabled = Column(Boolean, default=True)                     # 是否启用
    config = Column(JSON, default={})                           # 策略自定义配置
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class StrategyExecution(Base):
    """
    策略执行记录模型类
    
    记录每次策略执行的详细信息，用于后续分析和学习
    """
    
    __tablename__ = "strategy_executions"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(UUID(as_uuid=True))                     # 关联会话ID
    question = Column(String, nullable=False)                   # 用户问题
    final_decision = Column(Boolean)                            # 最终决策
    final_confidence = Column(Float)                            # 综合置信度
    strategy_results = Column(JSON)                             # 各策略执行结果
    used_knowledge_base = Column(Boolean)                      # 是否使用知识库
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class StrategyFeedback(Base):
    """
    策略反馈模型类
    
    收集用户对策略决策的反馈，用于动态调整策略权重
    """
    
    __tablename__ = "strategy_feedbacks"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    execution_id = Column(UUID(as_uuid=True), ForeignKey("strategy_executions.id"))
    strategy_name = Column(String)                              # 策略名称
    feedback_score = Column(Float)                              # 反馈分数（-1到1）
    reason = Column(String)                                     # 反馈原因
    created_at = Column(DateTime(timezone=True), server_default=func.now())
