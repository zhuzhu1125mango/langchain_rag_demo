"""文档检索路由：搜索、列表、预览、分块、来源定位、详情。"""

import asyncio
import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from src.auth import CurrentUser, get_current_user, require_owner
from src.config import settings
from src.database import get_db
from src.models import Document, KnowledgeBase
from src.services.document_processor import get_document_chunks, preview_document
from src.services.vector_store import VectorStoreManager
from src.utils.error_handling import ErrorSeverity, log_exception

from .common import (
    DocumentResponse,
    DocumentSourceResponse,
    SearchResult,
    get_owned_document as _get_owned_document,
    get_status_display,
    serialize_document,
)

logger = logging.getLogger("rag_system")

router = APIRouter(tags=["documents"])


@router.get("/search", response_model=List[SearchResult])
async def search_documents(
    query: str = Query(..., description="搜索关键词"),
    kb_id: Optional[str] = Query(None, description="知识库ID，不传则搜索所有知识库"),
    limit: Optional[int] = Query(10, ge=1, le=100, description="返回结果数量"),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user)
):
    if not query.strip():
        raise HTTPException(status_code=400, detail="搜索关键词不能为空")

    vector_store = await VectorStoreManager.get_instance()

    kb_ids = []
    if kb_id:
        try:
            kb_uuid = uuid.UUID(kb_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="无效的知识库ID")
        kb_result = await db.execute(select(KnowledgeBase).filter(KnowledgeBase.id == kb_uuid))
        kb = kb_result.scalar_one_or_none()
        if not kb:
            raise HTTPException(status_code=404, detail="知识库不存在")
        require_owner(kb.owner_id, current_user)
        kb_ids = [kb_id]
    else:
        # W6 #23：归属过滤下推到向量库——未指定 kb_id 时传当前用户全部 kb_ids，
        # 让 Milvus 在检索层过滤，避免 limit 名额被其他用户的文档占据后在应用层丢弃
        kb_rows = await db.execute(
            select(KnowledgeBase.id).filter(KnowledgeBase.owner_id == current_user.user_id)
        )
        kb_ids = [str(row[0]) for row in kb_rows.all()]
        if not kb_ids:
            return []

    try:
        results = await vector_store.search(query, k=limit, kb_ids=kb_ids)

        search_results = []

        # 收集所有唯一的 doc_id 和 kb_id，避免 N+1 查询
        unique_doc_ids = set()
        for doc in results:
            doc_id = doc.metadata.get("document_id", "")
            if doc_id:
                unique_doc_ids.add(doc_id)

        # 仅映射当前用户拥有的文档（跨用户命中结果直接丢弃，防止内容泄露）
        doc_id_to_name = {}
        if unique_doc_ids:
            doc_result = await db.execute(
                select(Document, KnowledgeBase.name.label("kb_name"))
                .outerjoin(KnowledgeBase, Document.kb_id == KnowledgeBase.id)
                .filter(
                    Document.id.in_([uuid.UUID(did) for did in unique_doc_ids]),
                    Document.owner_id == current_user.user_id
                )
            )
            for row in doc_result.all():
                document = row.Document
                kb_name = row.kb_name
                doc_id_to_name[str(document.id)] = {
                    "filename": document.filename,
                    "kb_id": str(document.kb_id) if document.kb_id else None,
                    "kb_name": kb_name
                }

        for doc in results:
            doc_id = doc.metadata.get("document_id", "")
            info = doc_id_to_name.get(doc_id)
            if info is None:
                # 非当前用户的文档，跳过（不返回内容摘要）
                continue

            content = doc.page_content
            highlight = None
            if query.lower() in content.lower():
                idx = content.lower().index(query.lower())
                start = max(0, idx - 30)
                end = min(len(content), idx + len(query) + 30)
                highlight = content[start:end]
                if start > 0:
                    highlight = "..." + highlight
                if end < len(content):
                    highlight = highlight + "..."

            search_results.append(SearchResult(
                id=doc_id,
                filename=info.get("filename", "未知文件"),
                kb_id=info.get("kb_id"),
                kb_name=info.get("kb_name"),
                content=doc.page_content[:500] + "..." if len(doc.page_content) > 500 else doc.page_content,
                score=doc.metadata.get("score", 0),
                highlight=highlight
            ))
        
        return search_results
    except Exception as e:
        log_exception(
            logger, e,
            action="document.search",
            message="文档检索失败",
            query_length=len(query or ""),
        )
        raise HTTPException(status_code=500, detail="搜索失败，请稍后重试")


@router.get("/", response_model=List[DocumentResponse])
async def list_documents(
    kb_id: Optional[str] = Query(None, description="知识库ID，不传则查询所有知识库的文档"),
    status: Optional[str] = Query(None, description="文档状态筛选：active（已上架）、inactive（已下架）"),
    limit: int = Query(200, ge=1, le=500, description="单页条数上限（1-500，默认 200）"),
    offset: int = Query(0, ge=0, description="偏移量（配合 limit 翻页）"),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    文档列表（支持 limit/offset 分页）。

    响应仍为扁平数组以保持前端兼容；无上限全表拉取已被 limit 硬顶（默认
    200，最大 500）。排序固定 created_at 降序，保证翻页窗口稳定。
    """
    try:
        logger.info(f"list_documents called with kb_id: {kb_id}, status: {status}")

        query = (
            select(Document)
            .options(
                joinedload(Document.knowledge_base),
                joinedload(Document.category),
            )
            .filter(Document.owner_id == current_user.user_id)
        )

        if kb_id:
            try:
                query = query.filter(Document.kb_id == uuid.UUID(kb_id))
                logger.info(f"Filtering by kb_id: {kb_id}")
            except ValueError as e:
                logger.error(f"Invalid kb_id format: {kb_id}")
                raise HTTPException(status_code=400, detail="无效的知识库ID")

        if status:
            if status == "active":
                query = query.filter(Document.status == "published")
                logger.info("Filtering by status: published")
            elif status == "inactive":
                query = query.filter(Document.status.in_(["draft", "archived"]))
                logger.info("Filtering by status: draft or archived")

        # 排序固定 created_at 降序（翻页窗口稳定），分页截断收尾
        query = query.order_by(Document.created_at.desc()).limit(limit).offset(offset)

        result = await db.execute(query)
        docs = result.scalars().all()
        logger.info(f"Found {len(docs)} documents")
        
        result = [
            DocumentResponse(
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
                quality_grade=doc.quality_grade or None
            ) for doc in docs
        ]
        
        logger.info(f"Returning {len(result)} documents")
        return result
        
    except Exception as e:
        # 原样上抛（交由全局异常处理），此处仅补齐结构化记录
        log_exception(
            logger, e,
            action="document.list",
            message="文档列表查询失败",
        )
        raise


@router.get("/{doc_id}/preview")
async def preview_doc(
    doc_id: str,
    page: int = 1,
    page_size: int = 500,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    doc = await _get_owned_document(db, doc_id, current_user)

    if page < 1:
        page = 1
    if page_size < 10 or page_size > 2000:
        page_size = 500

    try:
        result = await asyncio.to_thread(
            preview_document, doc.file_path, page=page, page_size=page_size
        )
        return {
            "doc_id": doc_id,
            "filename": doc.filename,
            **result
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # 预览失败原先完全静默（直接 500），排障时无迹可寻
        log_exception(
            logger, e,
            action="document.preview",
            message="文档预览失败",
            doc_id=doc_id,
        )
        raise HTTPException(status_code=500, detail="预览文档失败，请稍后重试")


@router.get("/{doc_id}/chunks")
async def get_doc_chunks(
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    doc = await _get_owned_document(db, doc_id, current_user)

    try:
        chunks = await asyncio.to_thread(get_document_chunks, doc.file_path)
        return {
            "doc_id": doc_id,
            "filename": doc.filename,
            "total_chunks": len(chunks),
            "chunks": chunks
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # 同上：原先静默返回 500
        log_exception(
            logger, e,
            action="document.chunks",
            message="获取文档文本块失败",
            doc_id=doc_id,
        )
        raise HTTPException(status_code=500, detail="获取文本块失败，请稍后重试")


@router.get("/{doc_id}/source/{chunk_index}", response_model=DocumentSourceResponse)
async def get_document_source(
    doc_id: str,
    chunk_index: int = Path(..., ge=0, description="文本块索引"),
    context_chunks: int = Query(1, ge=0, le=5, description="前后上下文块数"),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user)
):
    """
    获取文档特定文本块的内容（用于答案溯源）

    Args:
        doc_id: 文档ID
        chunk_index: 文本块索引
        context_chunks: 前后上下文块数

    Returns:
        DocumentSourceResponse: 包含文本块内容和上下文信息
    """
    doc = await _get_owned_document(db, doc_id, current_user)

    try:
        chunks = await asyncio.to_thread(get_document_chunks, doc.file_path)

        if chunk_index < 0 or chunk_index >= len(chunks):
            raise HTTPException(status_code=400, detail="无效的文本块索引")

        kb_name = None
        if doc.kb_id:
            kb_result = await db.execute(select(KnowledgeBase).filter(KnowledgeBase.id == doc.kb_id))
            kb = kb_result.scalar_one_or_none()
            kb_name = kb.name if kb else None

        start_idx = max(0, chunk_index - context_chunks)
        end_idx = min(len(chunks), chunk_index + context_chunks + 1)
        
        surrounding_content = "\n\n".join([c["content"] for c in chunks[start_idx:end_idx]])
        
        highlight_offset = 0
        for i in range(start_idx, chunk_index):
            highlight_offset += len(chunks[i]["content"]) + 2
        
        return DocumentSourceResponse(
            doc_id=doc_id,
            filename=doc.filename,
            kb_id=str(doc.kb_id) if doc.kb_id else None,
            kb_name=kb_name,
            chunk_index=chunk_index,
            total_chunks=len(chunks),
            content=chunks[chunk_index]["content"],
            surrounding_content=surrounding_content,
            highlight_offset=highlight_offset,
            highlight_length=len(chunks[chunk_index]["content"])
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log_exception(
            logger, e,
            action="document.source",
            message="获取文档来源失败",
            doc_id=doc_id,
            chunk_index=chunk_index,
        )
        raise HTTPException(status_code=500, detail="获取文档来源失败，请稍后重试")


@router.get("/{doc_id}", response_model=DocumentResponse)
async def get_document(
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    doc = await _get_owned_document(db, doc_id, current_user)
    return serialize_document(doc)
