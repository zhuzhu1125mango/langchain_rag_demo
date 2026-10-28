"""文档 API 共享层：请求/响应模型、归属校验、序列化（CODE-1 拆分）。

本模块不含路由；`upload` / `query` / `manage` 三组路由共用这里的模型与校验。
"""

import os
import uuid
from typing import List, Literal, Optional

from fastapi import HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from src.auth import CurrentUser, require_owner
from src.config import settings
from src.models import Document, KnowledgeBase


def ensure_upload_size(file: UploadFile) -> int:
    """校验上传大小上限并返回实测字节数。

    不信任客户端声明的 size，按服务端实际接收的字节数校验；
    超过 settings.processing.MAX_UPLOAD_SIZE_MB 时抛出 413。
    """
    file.file.seek(0, os.SEEK_END)
    actual_size = file.file.tell()
    file.file.seek(0)
    limit = settings.processing.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    if actual_size > limit:
        raise HTTPException(
            status_code=413,
            detail=(
                f"文件过大: 实测 {actual_size / 1024 / 1024:.1f} MB，"
                f"超过上限 {settings.processing.MAX_UPLOAD_SIZE_MB} MB"
            ),
        )
    return actual_size

async def get_owned_document(
    db: AsyncSession, doc_id: str, current_user: CurrentUser
) -> Document:
    """按 ID 加载文档并校验归属，统一所有单文档端点的鉴权路径。

    Raises:
        HTTPException: 400(ID 非法) / 404(不存在) / 403(非所有者)
    """
    try:
        doc_uuid = uuid.UUID(doc_id)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=400, detail="无效的文档ID")

    result = await db.execute(
        select(Document)
        .options(
            joinedload(Document.knowledge_base),
            joinedload(Document.category),
        )
        .filter(Document.id == doc_uuid)
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")

    require_owner(doc.owner_id, current_user)
    return doc

class DocumentResponse(BaseModel):
    """文档响应模型。"""
    id: str
    filename: str
    file_type: str
    size: int
    kb_id: Optional[str] = None
    kb_name: Optional[str] = None
    status: str
    processing_status: Optional[str] = None
    processing_message: Optional[str] = None
    processing_progress: Optional[int] = None
    chunks_count: int
    created_at: str
    category_name: Optional[str] = None
    tags: List[str] = []
    # 文档自动分析字段
    document_type: Optional[str] = None
    document_type_label: Optional[str] = None
    domain: Optional[str] = None
    domain_label: Optional[str] = None
    topics: List[str] = []
    summary: Optional[str] = None
    quality_score: Optional[int] = None
    quality_grade: Optional[str] = None


class DocumentUpdate(BaseModel):
    """文档更新请求模型。"""
    category_id: Optional[str] = None
    tags: Optional[List[str]] = None
    status: Optional[Literal["draft", "published", "archived"]] = None
    kb_id: Optional[str] = None


class BatchDeleteRequest(BaseModel):
    """批量删除请求模型。"""
    ids: List[str]


class DocumentSourceResponse(BaseModel):
    """文档来源响应模型。"""
    doc_id: str
    filename: str
    kb_id: Optional[str] = None
    kb_name: Optional[str] = None
    chunk_index: int
    total_chunks: int
    content: str
    surrounding_content: str
    highlight_offset: int
    highlight_length: int

async def resolve_target_kb(db: AsyncSession, kb_id: Optional[str], current_user: CurrentUser) -> KnowledgeBase:
    """解析上传目标知识库（单文件/批量上传共用，W6 #3）。

    显式 kb_id：校验格式、存在性、归属；缺省：回退当前用户默认知识库。

    Raises:
        HTTPException: 400 无效格式/不存在/无默认知识库；403 非归属。
    """
    if kb_id:
        try:
            kb_uuid = uuid.UUID(kb_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="无效的知识库ID")
        result = await db.execute(select(KnowledgeBase).filter(KnowledgeBase.id == kb_uuid))
        kb = result.scalars().first()
        if not kb:
            raise HTTPException(status_code=400, detail="知识库不存在")
        require_owner(kb.owner_id, current_user)
        return kb

    result = await db.execute(
        select(KnowledgeBase).filter(
            KnowledgeBase.is_default == True,
            KnowledgeBase.owner_id == current_user.user_id,
        )
    )
    kb = result.scalars().first()
    if not kb:
        raise HTTPException(status_code=400, detail="没有找到默认知识库，请先创建知识库")
    return kb

def get_status_display(status: str) -> str:
    status_map = {
        "published": "active",
        "draft": "inactive",
        "archived": "inactive"
    }
    return status_map.get(status, "inactive")

def serialize_document(doc: Document) -> DocumentResponse:
    """将文档 ORM 对象序列化为对外响应模型。

    集中转发字段映射，屏蔽 file_path/owner_id/quality_details 等内部字段。
    """
    return DocumentResponse(
        id=str(doc.id),
        filename=doc.filename,
        file_type=doc.file_type,
        size=doc.size,
        kb_id=str(doc.kb_id) if doc.kb_id else None,
        kb_name=doc.knowledge_base.name if doc.knowledge_base else None,
        status=get_status_display(doc.status),
        processing_status=doc.processing_status,
        processing_message=doc.processing_message,
        processing_progress=doc.processing_progress,
        chunks_count=doc.chunks_count,
        created_at=doc.created_at.isoformat(),
        category_name=doc.category.name if doc.category else None,
        tags=doc.tags if doc.tags else [],
        document_type=doc.document_type or None,
        document_type_label=doc.document_type_label or None,
        domain=doc.domain or None,
        domain_label=doc.domain_label or None,
        topics=doc.topics if doc.topics else [],
        summary=doc.summary or None,
        quality_score=doc.quality_score if doc.quality_score else None,
        quality_grade=doc.quality_grade or None,
    )

class SearchResult(BaseModel):
    """文档搜索结果模型。"""
    id: str
    filename: str
    kb_id: Optional[str] = None
    kb_name: Optional[str] = None
    content: str
    score: float
    highlight: Optional[str] = None

class DuplicateDetectionResult(BaseModel):
    """重复检测结果模型"""
    document_id: str
    filename: str
    similarity: float
    kb_id: str
    kb_name: Optional[str] = None
