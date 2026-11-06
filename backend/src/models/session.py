"""
会话模型 - Session

定义聊天会话数据模型，用于存储会话历史和上下文信息
"""

from sqlalchemy import Column, String, DateTime, Index, JSON, UUID, text
from sqlalchemy.dialects.postgresql import JSONB
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

    # 会话列表热路径：WHERE user_id = ? ORDER BY updated_at DESC。
    # 必须与 alembic 迁移 e6f7a8b9c0d1 保持一致，否则 autogenerate 会一直报漂移。
    __table_args__ = (
        Index("ix_sessions_user_id_updated_at", "user_id", text("updated_at DESC")),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)  # 会话唯一标识
    user_id = Column(String, nullable=False)                               # 用户标识
    title = Column(String)                                                # 会话标题
    # 消息历史（JSONB：支持 jsonb || 原子追加，见 session_service.append_session_message）。
    # 旧库为 JSON 列，需执行 scripts/migrate_session_messages_jsonb.py 迁移。
    messages = Column(JSONB, default=list)
    kb_ids = Column(JSON, default=[])                                     # 关联的知识库ID列表
    created_at = Column(DateTime(timezone=True), server_default=func.now()) # 创建时间
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())  # 更新时间