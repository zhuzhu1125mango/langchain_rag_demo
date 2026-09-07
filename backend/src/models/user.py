"""
用户模型 - User

JWT 多用户认证（P1-1）的用户表。id 即资源 owner_id，
知识库/文档/会话等资源按该字段做对象级隔离。
"""

from sqlalchemy import Column, String, DateTime, UUID
from sqlalchemy.sql import func
import uuid
from src.database import Base


class User(Base):
    """用户模型类。"""

    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username = Column(String(64), nullable=False, unique=True, index=True)
    # bcrypt 哈希（60 字符），预留 128 以兼容未来算法切换
    password_hash = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
