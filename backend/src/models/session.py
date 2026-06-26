"""
会话模型 - Session

定义聊天会话数据模型，用于存储会话历史和上下文信息
"""

from sqlalchemy import Column, String, DateTime, JSON, UUID
from sqlalchemy.sql import func
import uuid
from src.database import Base


class Session(Base):
    """
    会话模型类
    
    存储聊天会话信息，包括消息历史、关联知识库等
    支持多轮上下文对话功能
    """
    
    __tablename__ = "sessions"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)  # 会话唯一标识
    user_id = Column(String, nullable=False)                               # 用户标识
    title = Column(String)                                                # 会话标题
    messages = Column(JSON, default=[])                                   # 消息历史（JSON数组）
    kb_ids = Column(JSON, default=[])                                     # 关联的知识库ID列表
    created_at = Column(DateTime(timezone=True), server_default=func.now()) # 创建时间
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())  # 更新时间