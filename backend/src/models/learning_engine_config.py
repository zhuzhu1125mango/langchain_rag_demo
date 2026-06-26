"""
学习引擎配置模型 - LearningEngineConfig

持久化学习引擎的启用状态、学习间隔和上次学习时间
"""

from sqlalchemy import Column, Boolean, DateTime, Integer
from sqlalchemy.sql import func
from sqlalchemy.dialects.postgresql import UUID
import uuid
from src.database import Base


class LearningEngineConfig(Base):
    """
    学习引擎配置模型类

    存储学习引擎的全局配置，表内通常只有一条记录
    """

    __tablename__ = "learning_engine_configs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    enabled = Column(Boolean, default=True)
    learning_interval_hours = Column(Integer, default=24)
    last_learning_time = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
