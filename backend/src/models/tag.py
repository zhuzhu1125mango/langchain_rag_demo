"""
标签模型 - Tag

定义文档标签数据模型，用于标记和分类文档
"""

from sqlalchemy import Column, String, UUID, DateTime
from sqlalchemy.sql import func
import uuid
from src.database import Base


class Tag(Base):
    """
    标签模型类
    
    用于标记文档，支持颜色标识，便于快速识别
    """
    
    __tablename__ = "tags"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)  # 标签唯一标识
    name = Column(String, nullable=False, unique=True)                     # 标签名称（唯一）
    color = Column(String, default="#1890ff")                             # 标签颜色（十六进制）
    created_at = Column(DateTime(timezone=True), server_default=func.now()) # 创建时间