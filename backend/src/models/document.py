"""
文档模型 - Document

定义文档数据模型，用于存储上传文档的元数据信息
"""

from sqlalchemy import Column, String, Integer, DateTime, Enum, JSON, ForeignKey, UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import uuid
from src.database import Base


class Document(Base):
    """
    文档模型类
    
    存储上传文档的元数据，包括文件名、路径、类型、大小、分类、标签等信息
    """
    
    __tablename__ = "documents"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)  # 文档唯一标识
    filename = Column(String, nullable=False)                              # 文件名
    file_path = Column(String, nullable=False)                             # 文件存储路径
    file_type = Column(String, nullable=False)                             # 文件类型（扩展名）
    size = Column(Integer)                                                # 文件大小（字节）
    kb_id = Column(UUID(as_uuid=True), ForeignKey("knowledge_bases.id"), index=True)   # 所属知识库ID（带索引）
    owner_id = Column(String, nullable=False, default="default")          # 资源所有者标识
    category_id = Column(UUID(as_uuid=True), ForeignKey("categories.id"))  # 所属分类ID
    
    knowledge_base = relationship("KnowledgeBase", backref="kb_documents")  # 关联的知识库
    category = relationship("Category", backref="cat_documents")         # 关联的分类
    tags = Column(JSON, default=[])                                       # 标签列表（JSON数组）
    status = Column(Enum("draft", "published", "archived", name="document_status"), default="draft")  # 状态：草稿/发布/归档
    processing_status = Column(Enum("pending", "uploading", "processing", "completed", "failed", name="processing_status"), default="pending")  # 处理状态
    processing_message = Column(String, default="")                        # 处理状态消息
    processing_progress = Column(Integer, default=0)                       # 处理进度(0-100)
    chunks_count = Column(Integer, default=0)                             # 文本分片数量

    # 文档自动分析字段（AB方案：规则+LLM）
    document_type = Column(String, default="")                            # 文档类型编码
    document_type_label = Column(String, default="")                      # 文档类型显示标签
    domain = Column(String, default="")                                   # 领域编码
    domain_label = Column(String, default="")                             # 领域显示标签
    topics = Column(JSON, default=[])                                     # 主题标签（LLM分析结果）
    summary = Column(String, default="")                                  # 内容摘要
    quality_score = Column(Integer, default=0)                            # 综合质量评分(0-100)
    quality_grade = Column(String, default="")                            # 质量等级（优秀/良好/中等/及格/需改进）
    quality_details = Column(JSON, default={})                            # 详细质量评分JSON

    created_at = Column(DateTime(timezone=True), server_default=func.now())  # 创建时间
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())     # 更新时间