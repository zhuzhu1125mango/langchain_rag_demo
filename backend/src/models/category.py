"""
分类模型 - Category

定义文档分类数据模型，支持多级分类结构
"""

from sqlalchemy import Column, String, Integer, ForeignKey, UUID, DateTime
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
import uuid
from src.database import Base


class Category(Base):
    """
    分类模型类
    
    用于组织和管理文档分类，支持多级分类（通过parent_id实现）
    """
    
    __tablename__ = "categories"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)  # 分类唯一标识
    name = Column(String, nullable=False, unique=True)                     # 分类名称（唯一）
    description = Column(String)                                          # 分类描述
    parent_id = Column(UUID(as_uuid=True), ForeignKey("categories.id"))    # 父分类ID（支持多级）
    sort_order = Column(Integer, default=0)                               # 排序顺序
    created_at = Column(DateTime(timezone=True), server_default=func.now()) # 创建时间
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())      # 更新时间
    
    # 关系定义
    parent = relationship("Category", remote_side=[id])  # 自引用父分类