"""
知识库模型 - KnowledgeBase

定义知识库数据模型，用于存储知识库的基本信息
"""

from sqlalchemy import Column, String, DateTime, UUID, Boolean
from sqlalchemy.sql import func
import uuid
from src.database import Base


class KnowledgeBase(Base):
    """
    知识库模型类

    存储知识库的基本信息，支持多知识库隔离管理
    """

    __tablename__ = "knowledge_bases"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    embedding_model = Column(String, default="bge-m3:latest")
    is_default = Column(Boolean, default=False)
    status = Column(String, default="active")
    owner_id = Column(String, nullable=False, default="default")  # 资源所有者标识
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())