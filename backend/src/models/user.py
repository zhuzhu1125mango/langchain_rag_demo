"""
用户模型 - User

JWT 多用户认证（P1-1）的用户表。id 即资源 owner_id，
知识库/文档/会话等资源按该字段做对象级隔离。
"""

from sqlalchemy import Boolean, Column, String, DateTime, UUID, false
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
    # 管理员身份（P0-2）：权限挂在「人」上，而不是一把全局 ADMIN_KEY。
    # 全局 Key 仍保留，但只作为应急引导通道（首次部署/管理员账号不可用时）。
    is_admin = Column(
        Boolean, nullable=False, default=False, server_default=false(), index=True
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())
