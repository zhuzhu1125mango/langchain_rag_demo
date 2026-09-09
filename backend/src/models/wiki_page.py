"""Wiki 页面模型 - WikiPage（P2 LLM-Wiki 编译层 Phase 1）。

存储 LLM 编译产出的实体页/主题页/索引页元信息，页面正文持久化在 MinIO
（wiki/{kb_id}/{page_id}.md），本表仅存映射与版本信息。
"""

import uuid

from sqlalchemy import Column, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from src.database import Base


class WikiPage(Base):
    """Wiki 页面元数据。

    - source_doc_ids 记录贡献该页的源文档，Phase 2 级联更新的依据
    - (kb_id, page_type, title) 唯一，作为幂等 upsert 依据
    """

    __tablename__ = "wiki_pages"
    __table_args__ = (
        UniqueConstraint("kb_id", "page_type", "title", name="uq_wiki_pages_kb_type_title"),
    )

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    kb_id = Column(String(36), nullable=False, index=True)
    # entity / topic / index
    page_type = Column(String(16), nullable=False)
    title = Column(String(256), nullable=False)
    # MinIO 对象 key：wiki/{kb_id}/{page_id}.md
    content_path = Column(String(512), nullable=False)
    # 贡献该页的源文档 id 列表
    source_doc_ids = Column(JSONB, nullable=False, default=list)
    revision = Column(Integer, nullable=False, default=1)
    # active / stale / deleted
    status = Column(String(16), nullable=False, default="active")
    owner_id = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
