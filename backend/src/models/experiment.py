"""
A/B测试实验模型

定义实验配置、变体和分流记录的数据模型
"""

from sqlalchemy import Column, String, Float, Boolean, DateTime, JSON, UUID, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import uuid
from src.database import Base


class Experiment(Base):
    """
    实验模型类
    
    存储A/B测试实验的配置信息
    """
    
    __tablename__ = "experiments"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)                        # 实验名称
    description = Column(String)                                 # 实验描述
    status = Column(String, default="created")                   # 状态: created, running, stopped
    variants = Column(JSON, nullable=False)                      # 变体配置
    metrics = Column(JSON, default=["accuracy", "response_time"]) # 监控指标
    traffic_allocation = Column(Float, default=1.0)              # 流量分配比例
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    started_at = Column(DateTime(timezone=True))
    ended_at = Column(DateTime(timezone=True))

    # 级联关联：删除实验时同步清理关联数据
    experiment_variants = relationship(
        "ExperimentVariant",
        back_populates="experiment",
        cascade="all, delete-orphan",
        passive_deletes=True
    )
    traffic_allocations = relationship(
        "TrafficAllocation",
        back_populates="experiment",
        cascade="all, delete-orphan",
        passive_deletes=True
    )
    experiment_metrics = relationship(
        "ExperimentMetric",
        back_populates="experiment",
        cascade="all, delete-orphan",
        passive_deletes=True
    )
    experiment_results = relationship(
        "ExperimentResult",
        back_populates="experiment",
        cascade="all, delete-orphan",
        passive_deletes=True
    )


class ExperimentVariant(Base):
    """
    实验变体模型类
    
    存储每个变体的详细配置
    """
    
    __tablename__ = "experiment_variants"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    experiment_id = Column(
        UUID(as_uuid=True),
        ForeignKey("experiments.id", ondelete="CASCADE"),
        nullable=False
    )                                                            # 关联实验ID
    name = Column(String, nullable=False)                        # 变体名称
    weight = Column(Float, default=0.5)                         # 流量权重
    config = Column(JSON, nullable=False)                        # 变体配置
    enabled = Column(Boolean, default=True)                      # 是否启用

    experiment = relationship("Experiment", back_populates="experiment_variants")


class TrafficAllocation(Base):
    """
    流量分配记录模型类
    
    记录用户被分配到哪个变体
    """
    
    __tablename__ = "traffic_allocations"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    experiment_id = Column(
        UUID(as_uuid=True),
        ForeignKey("experiments.id", ondelete="CASCADE"),
        nullable=False
    )                                                            # 关联实验ID
    variant_id = Column(String)                                  # 关联变体ID（兼容自定义字符串ID）
    user_id = Column(String)                                     # 用户标识
    session_id = Column(String)                                  # 会话ID
    allocated_at = Column(DateTime(timezone=True), server_default=func.now())

    experiment = relationship("Experiment", back_populates="traffic_allocations")


class ExperimentMetric(Base):
    """
    实验指标记录模型类
    
    记录每个变体的指标数据
    """
    
    __tablename__ = "experiment_metrics"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    experiment_id = Column(
        UUID(as_uuid=True),
        ForeignKey("experiments.id", ondelete="CASCADE"),
        nullable=False
    )                                                            # 关联实验ID
    variant_id = Column(String)                                  # 关联变体ID（兼容自定义字符串ID）
    metric_name = Column(String, nullable=False)                 # 指标名称
    metric_value = Column(Float, nullable=False)                 # 指标值
    recorded_at = Column(DateTime(timezone=True), server_default=func.now())

    experiment = relationship("Experiment", back_populates="experiment_metrics")


class ExperimentResult(Base):
    """
    实验结果模型类
    
    存储实验的最终分析结果
    """
    
    __tablename__ = "experiment_results"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    experiment_id = Column(
        UUID(as_uuid=True),
        ForeignKey("experiments.id", ondelete="CASCADE"),
        nullable=False
    )                                                            # 关联实验ID
    winning_variant_id = Column(String)                          # 获胜变体ID（兼容自定义字符串ID）
    analysis_data = Column(JSON)                                 # 分析数据
    confidence = Column(Float)                                   # 置信度
    conclusion = Column(String)                                  # 结论
    analyzed_at = Column(DateTime(timezone=True), server_default=func.now())

    experiment = relationship("Experiment", back_populates="experiment_results")