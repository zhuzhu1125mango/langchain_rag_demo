"""审计日志模型。

记录敏感操作（文档上传/删除、知识库增删、配置变更、用户注册等），
与调试性质的 request_trace 分离。请求审计接口需管理员权限（require_admin）。
"""

import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, JSON, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from src.database import Base

# PG 上使用 JSONB（支持 jsonb 运算符/函数），测试用 SQLite 回退 JSON（同 request_trace）
JSONType = JSON().with_variant(JSONB(), "postgresql")


class AuditLog(Base):
    """审计日志表。"""

    __tablename__ = "audit_logs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(64), nullable=True, index=True)
    username = Column(String(128), nullable=True)
    action = Column(String(64), nullable=False, index=True)     # 如 document.upload / kb.delete
    resource_type = Column(String(32), nullable=True, index=True)
    resource_id = Column(String(64), nullable=True)
    detail = Column(JSONType, nullable=True)                     # 结构化附加上下文
    ip_address = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)