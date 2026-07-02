"""
文档管理API - Document API

提供文档的CRUD操作接口，包括：
1. 单文件上传（支持WebSocket进度推送，已改为异步处理）
2. 批量文件上传
3. 文档列表查询
4. 文档详情查询
5. 文档更新（分类、标签、状态）
6. 文档删除（已改为异步处理）
7. 文档状态更新（上下架）
8. 上传进度查询
"""

import logging
import sys
import asyncio
from fastapi import APIRouter, UploadFile, File, Depends, HTTPException, Query, Body, WebSocket, WebSocketDisconnect, Path, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func as sql_func
from sqlalchemy.orm import joinedload
from pydantic import BaseModel
from typing import List, Optional
import os
import re
import math
import uuid
from collections import Counter
from src.database import get_db, async_session_maker
from src.auth import get_current_user, CurrentUser, require_owner
from src.models import Document, Category, KnowledgeBase

logger = logging.getLogger("rag_system")
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)
from src.services.document_processor import process_document, save_uploaded_file, SUPPORTED_EXTENSIONS, preview_document, get_document_chunks
from src.services.vector_store import VectorStoreManager
from src.services.rag_chain import RAGChain
from src.services.document_analyzer import DocumentAnalyzer
from src.schemas.document import DuplicateDetectionRequest, DocumentQualityResponse, DocumentClassificationResponse
from src.services.progress_manager import (
    create_upload_progress,
    update_upload_progress,
    remove_upload_progress,
    register_ws_connection,
    unregister_ws_connection,
    get_upload_progress
)
from src.services.notification_service import (
    notify_doc_list_changed,
    notify_task_progress,
    notify_task_completed,
    notify_task_failed
)
from src.config import settings

router = APIRouter(prefix="/documents", tags=["documents"])


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
    status: Optional[str] = None
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


async def get_default_kb_id(db: AsyncSession) -> str:
    result = await db.execute(select(KnowledgeBase).filter(KnowledgeBase.is_default == True))
    kb = result.scalars().first()
    if kb:
        return str(kb.id)
    return ""


async def process_document_async(
    doc_id: str,
    file_path: str,
    kb_id: str,
    task_upload_id: str,
    chunk_size: Optional[int],
    chunk_overlap: Optional[int]
):
    """
    异步处理文档（后台任务）

    在独立的事务中处理文档：
    1. 解析文档
    2. 向量化
    3. 保存到向量数据库
    4. 更新文档状态
    """
    async with async_session_maker() as db:
        try:
            from src.services.minio_service import MinioService

            # 获取文档记录
            result = await db.execute(select(Document).filter(Document.id == uuid.UUID(doc_id)))
            doc = result.scalars().first()

            if not doc:
                await notify_task_failed(task_upload_id, "文档记录不存在")
                return

            # 阶段1：解析文档 (0-30%)
            doc.processing_message = "正在解析文档..."
            doc.processing_progress = 10
            await db.commit()
            await notify_task_progress(task_upload_id, 10, "正在解析文档...")

            chunks = process_document(file_path, chunk_size, chunk_overlap)

            # 为每个 chunk 补充 document_id 元数据，确保向量能正确关联到文档
            for chunk in chunks:
                chunk.metadata["document_id"] = doc_id

            doc.processing_progress = 30
            await db.commit()
            await notify_task_progress(task_upload_id, 30, f"文档解析完成，共{len(chunks)}个文本块")

            # 阶段2：向量化 (30-70%)
            doc.processing_message = "正在向量化..."
            doc.processing_progress = 40
            await db.commit()
            await notify_task_progress(task_upload_id, 40, "正在生成向量...")

            vector_store = await VectorStoreManager.get_instance()
            await vector_store.add_documents(chunks, kb_id)

            doc.processing_progress = 70
            await db.commit()
            await notify_task_progress(task_upload_id, 70, "向量生成完成")

            # 阶段3：保存向量 (70-80%)
            doc.processing_message = "正在保存向量..."
            doc.processing_progress = 80
            await db.commit()
            await notify_task_progress(task_upload_id, 80, "正在保存向量...")

            await vector_store.save_vector_store()

            # 阶段4：规则分析 (80-90%)
            doc.processing_message = "正在分析文档内容..."
            doc.processing_progress = 85
            await db.commit()
            await notify_task_progress(task_upload_id, 85, "正在分析文档内容...")

            # 提取文档完整内容进行规则分析
            full_content = "\n\n".join(chunk.page_content for chunk in chunks if hasattr(chunk, 'page_content'))
            if not full_content:
                full_content = "\n\n".join(str(chunk) for chunk in chunks)

            # 规则分析：快速分类
            document_type, topics, domain = DocumentAnalyzer.analyze_document_content(full_content)
            # 规则分析：质量评估
            quality_result = DocumentAnalyzer.evaluate_quality(full_content)

            # 类型和领域标签映射
            type_labels = {
                "technical": "技术文档",
                "business": "业务文档",
                "report": "报告文档",
                "manual": "操作手册",
                "policy": "政策文件",
                "news": "新闻资讯",
                "other": "其他文档"
            }
            domain_labels = {
                "it": "信息技术",
                "finance": "金融",
                "healthcare": "医疗健康",
                "education": "教育",
                "legal": "法律",
                "government": "政府",
                "enterprise": "企业管理",
                "other": "其他领域"
            }

            # 保存规则分析结果
            doc.document_type = document_type
            doc.document_type_label = type_labels.get(document_type, "其他文档")
            doc.domain = domain
            doc.domain_label = domain_labels.get(domain, "其他领域")
            doc.topics = topics if topics else []
            doc.quality_score = int(quality_result.get("overall_score", 0))
            doc.quality_grade = quality_result.get("overall_grade", "")
            doc.quality_details = {
                "completeness": quality_result.get("completeness", 0),
                "readability": quality_result.get("readability", 0),
                "structure": quality_result.get("structure", 0),
                "relevance": quality_result.get("relevance", 0),
                "completeness_comment": quality_result.get("completeness_comment", ""),
                "readability_comment": quality_result.get("readability_comment", ""),
                "structure_comment": quality_result.get("structure_comment", ""),
                "relevance_comment": quality_result.get("relevance_comment", ""),
                "suggestions": quality_result.get("suggestions", [])
            }
            await db.commit()

            # 阶段5：LLM增强分析 (90-95%)
            doc.processing_message = "正在进行智能分析..."
            doc.processing_progress = 92
            await db.commit()
            await notify_task_progress(task_upload_id, 92, "正在进行智能分析...")

            try:
                llm_classify_result = await classify_document_with_llm(full_content)
                if llm_classify_result:
                    # LLM分类成功，覆盖规则分析结果
                    doc.document_type_label = llm_classify_result.get("document_type", doc.document_type_label)
                    doc.topics = llm_classify_result.get("topics", doc.topics)
                    doc.domain_label = llm_classify_result.get("domain", doc.domain_label)
                    doc.summary = llm_classify_result.get("summary", "")

                llm_quality_result = await evaluate_quality_with_llm(full_content)
                if llm_quality_result:
                    doc.quality_score = int(llm_quality_result.get("overall_score", doc.quality_score))
                    doc.quality_grade = llm_quality_result.get("overall_grade", doc.quality_grade)
                    doc.quality_details = llm_quality_result

                await db.commit()
                logger.info(f"文档LLM智能分析完成: {doc.filename}")
            except Exception as e:
                logger.warning(f"文档LLM分析失败，保留规则分析结果: {doc.filename}, 错误: {str(e)}")

            # 阶段6：完成 (95-100%)
            doc.status = "published"
            doc.processing_status = "completed"
            doc.processing_message = "处理完成"
            doc.processing_progress = 100
            doc.chunks_count = len(chunks)
            await db.commit()

            # 发送完成通知
            await notify_task_completed(task_upload_id, {
                "doc_id": doc_id,
                "filename": doc.filename,
                "chunks_count": len(chunks)
            })

            # 通知文档列表已更新
            await notify_doc_list_changed(kb_id, doc_id, "created")

            logger.info(f"文档处理完成: {doc.filename}, 共{len(chunks)}个文本块")

        except Exception as e:
            logger.error(f"文档处理失败: {doc_id}, 错误: {str(e)}", exc_info=True)

            # 更新文档状态为失败
            try:
                result = await db.execute(select(Document).filter(Document.id == uuid.UUID(doc_id)))
                doc = result.scalars().first()
                if doc:
                    doc.processing_status = "failed"
                    doc.processing_message = f"处理失败: {str(e)}"
                    doc.processing_progress = 0
                    await db.commit()
            except Exception:
                pass

            # 发送失败通知
            await notify_task_failed(task_upload_id, str(e))


async def process_document_delete_async(doc_id: str, kb_id: str, file_path: str, task_id: str):
    """
    异步删除文档（后台任务）

    在独立的事务中删除：
    1. 从MinIO删除文件
    2. 从向量数据库删除向量
    3. 从数据库删除记录
    """
    async with async_session_maker() as db:
        try:
            # 阶段1：删除MinIO文件 (0-30%)
            await notify_task_progress(task_id, 10, "正在删除文件...")
            if file_path.startswith("minio://"):
                from src.services.minio_service import MinioService
                minio_service = MinioService()
                minio_service.delete_file(file_path)
            elif os.path.exists(file_path):
                os.remove(file_path)

            await notify_task_progress(task_id, 30, "文件已删除")

            # 阶段2：删除向量 (30-70%)
            await notify_task_progress(task_id, 50, "正在删除向量...")
            vector_store = await VectorStoreManager.get_instance()
            await vector_store.delete_by_document_id(doc_id)
            await vector_store.save_vector_store()

            await notify_task_progress(task_id, 80, "向量已删除")

            # 阶段3：删除数据库记录 (70-100%)
            result = await db.execute(select(Document).filter(Document.id == uuid.UUID(doc_id)))
            doc = result.scalars().first()
            if doc:
                await db.delete(doc)
                await db.commit()

            await notify_task_progress(task_id, 100, "删除完成")

            # 发送完成通知
            await notify_task_completed(task_id, {
                "doc_id": doc_id,
                "status": "deleted"
            })

            # 通知文档列表已更新
            await notify_doc_list_changed(kb_id, doc_id, "deleted")

            logger.info(f"文档删除完成: {doc_id}")

        except Exception as e:
            logger.error(f"文档删除失败: {doc_id}, 错误: {str(e)}", exc_info=True)
            await notify_task_failed(task_id, str(e))


@router.post("/upload")
async def upload_document(
    file: UploadFile = File(...),
    kb_id: Optional[str] = Query(None, description="知识库ID，不传则使用默认知识库"),
    chunk_size: Optional[int] = Query(None, ge=50, le=2000, description="文本块大小（50-2000）"),
    chunk_overlap: Optional[int] = Query(None, ge=0, le=500, description="文本块重叠大小（0-500）"),
    upload_id: Optional[str] = Query(None, description="上传任务ID（用于WebSocket进度推送）"),
    background_tasks: BackgroundTasks = None,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    上传文档（异步处理）

    此接口会：
    1. 立即返回任务ID和文档ID
    2. 在后台异步处理文档（解析、向量化）
    3. 通过WebSocket实时推送处理进度
    """
    _, ext = os.path.splitext(file.filename)
    if ext.lower() not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型: {ext}")

    # 验证知识库ID
    kb_uuid = None
    if kb_id:
        try:
            kb_uuid = uuid.UUID(kb_id)
            result = await db.execute(select(KnowledgeBase).filter(KnowledgeBase.id == kb_uuid))
            kb = result.scalars().first()
            if not kb:
                raise HTTPException(status_code=400, detail="知识库不存在")
            require_owner(kb.owner_id, current_user)
        except ValueError:
            raise HTTPException(status_code=400, detail="无效的知识库ID")
    else:
        # 使用当前用户的默认知识库
        result = await db.execute(
            select(KnowledgeBase).filter(
                KnowledgeBase.is_default == True,
                KnowledgeBase.owner_id == current_user.user_id,
            )
        )
        kb = result.scalars().first()
        if not kb:
            raise HTTPException(status_code=400, detail="没有找到默认知识库，请先创建知识库")
        kb_uuid = kb.id

    # 生成上传ID（如果没有提供）
    task_upload_id = upload_id if upload_id else str(uuid.uuid4())
    doc_id = uuid.uuid4()

    try:
        from src.services.minio_service import MinioService
        minio_service = MinioService()

        # 更新状态：开始上传
        create_upload_progress(task_upload_id, file.filename, file.size)
        update_upload_progress(task_upload_id, status="uploading", message="正在上传文件...")

        # 先创建文档记录，标记为上传中
        doc = Document(
            id=doc_id,
            filename=file.filename,
            file_path="",
            file_type=ext.lower(),
            size=file.size,
            kb_id=kb_uuid,
            owner_id=current_user.user_id,
            status="draft",
            processing_status="uploading",
            processing_message="正在上传文件...",
            processing_progress=0,
            chunks_count=0,
            tags=[]
        )
        db.add(doc)
        await db.commit()

        # 上传文件到 MinIO
        file_path = minio_service.upload_file(file, doc_id)

        # 更新文档记录
        doc.file_path = file_path
        doc.processing_status = "processing"
        doc.processing_message = "正在处理文档..."
        doc.processing_progress = 5
        await db.commit()

        # 立即返回，后台处理
        update_upload_progress(task_upload_id, status="processing", message="正在处理文档...")

        # 将文档处理添加到后台任务
        background_tasks.add_task(
            process_document_async,
            str(doc_id),
            file_path,
            str(kb_uuid),
            task_upload_id,
            chunk_size,
            chunk_overlap
        )

        # 通知文档列表即将更新
        await notify_doc_list_changed(str(kb_uuid), str(doc_id), "created")

        return {
            "id": str(doc_id),
            "message": "文件上传成功，正在后台处理",
            "upload_id": task_upload_id,
            "status": "processing"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"上传文件失败: {str(e)}", exc_info=True)
        update_upload_progress(task_upload_id, status="failed", message=f"上传失败: {str(e)}")
        await notify_task_failed(task_upload_id, str(e))
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"上传文件失败: {str(e)}")


@router.post("/batch")
async def batch_upload(
    files: List[UploadFile] = File(...),
    kb_id: Optional[str] = Query(None, description="知识库ID，不传则使用默认知识库"),
    chunk_size: Optional[int] = Query(None, ge=50, le=2000, description="文本块大小（50-2000）"),
    chunk_overlap: Optional[int] = Query(None, ge=0, le=500, description="文本块重叠大小（0-500）"),
    background_tasks: BackgroundTasks = None,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    if kb_id:
        try:
            kb_uuid = uuid.UUID(kb_id)
            result = await db.execute(select(KnowledgeBase).filter(KnowledgeBase.id == kb_uuid))
            kb = result.scalars().first()
            if not kb:
                raise HTTPException(status_code=400, detail="知识库不存在")
            require_owner(kb.owner_id, current_user)
        except ValueError:
            raise HTTPException(status_code=400, detail="无效的知识库ID")
    else:
        result = await db.execute(
            select(KnowledgeBase).filter(
                KnowledgeBase.is_default == True,
                KnowledgeBase.owner_id == current_user.user_id,
            )
        )
        kb = result.scalars().first()
        if not kb:
            raise HTTPException(status_code=400, detail="没有找到默认知识库，请先创建知识库")
        kb_id = str(kb.id)

    from src.services.minio_service import MinioService

    results = []
    minio_service = MinioService()

    for file in files:
        _, ext = os.path.splitext(file.filename)
        if ext.lower() not in SUPPORTED_EXTENSIONS:
            results.append({"filename": file.filename, "status": "failed", "reason": "不支持的文件类型"})
            continue

        try:
            doc_id = uuid.uuid4()
            file_path = minio_service.upload_file(file, doc_id)

            # 创建文档记录，标记为处理中
            doc = Document(
                id=doc_id,
                filename=file.filename,
                file_path=file_path,
                file_type=ext.lower(),
                size=file.size,
                kb_id=uuid.UUID(kb_id) if kb_id else None,
                owner_id=current_user.user_id,
                status="draft",
                processing_status="processing",
                processing_message="正在处理文档...",
                processing_progress=5,
                chunks_count=0,
                tags=[]
            )
            db.add(doc)
            await db.commit()

            task_upload_id = f"batch_{doc_id}"
            create_upload_progress(task_upload_id, file.filename, file.size)
            update_upload_progress(task_upload_id, status="processing", message="正在处理文档...")

            # 将文档处理添加到后台任务
            background_tasks.add_task(
                process_document_async,
                str(doc_id),
                file_path,
                str(kb_id) if kb_id else "",
                task_upload_id,
                chunk_size,
                chunk_overlap
            )

            results.append({
                "filename": file.filename,
                "status": "processing",
                "doc_id": str(doc_id),
                "upload_id": task_upload_id
            })
        except Exception as e:
            results.append({"filename": file.filename, "status": "failed", "reason": str(e)})

    return {"results": results}


@router.get("/chunk-config")
async def get_chunk_config():
    return {
        "chunk_size": settings.processing.CHUNK_SIZE,
        "chunk_overlap": settings.processing.CHUNK_OVERLAP,
        "supported_range": {
            "chunk_size": {"min": 50, "max": 2000, "default": settings.processing.CHUNK_SIZE},
            "chunk_overlap": {"min": 0, "max": 500, "default": settings.processing.CHUNK_OVERLAP}
        },
        "recommendation": {
            "small_documents": {"chunk_size": 300, "chunk_overlap": 30},
            "medium_documents": {"chunk_size": 500, "chunk_overlap": 50},
            "large_documents": {"chunk_size": 800, "chunk_overlap": 80}
        }
    }


def get_status_display(status: str) -> str:
    status_map = {
        "published": "active",
        "draft": "inactive",
        "archived": "inactive"
    }
    return status_map.get(status, "inactive")


class SearchResult(BaseModel):
    """文档搜索结果模型。"""
    id: str
    filename: str
    kb_id: Optional[str] = None
    kb_name: Optional[str] = None
    content: str
    score: float
    highlight: Optional[str] = None


@router.get("/search", response_model=List[SearchResult])
async def search_documents(
    query: str = Query(..., description="搜索关键词"),
    kb_id: Optional[str] = Query(None, description="知识库ID，不传则搜索所有知识库"),
    limit: Optional[int] = Query(10, ge=1, le=100, description="返回结果数量"),
    db: AsyncSession = Depends(get_db)
):
    if not query.strip():
        raise HTTPException(status_code=400, detail="搜索关键词不能为空")

    vector_store = await VectorStoreManager.get_instance()

    kb_ids = []
    if kb_id:
        try:
            kb_uuid = uuid.UUID(kb_id)
            kb_ids = [kb_id]
        except ValueError:
            raise HTTPException(status_code=400, detail="无效的知识库ID")

    try:
        results = await vector_store.search(query, k=limit, kb_ids=kb_ids if kb_ids else None)

        search_results = []

        # 收集所有唯一的 doc_id 和 kb_id，避免 N+1 查询
        unique_doc_ids = set()
        for doc in results:
            doc_id = doc.metadata.get("document_id", "")
            if doc_id:
                unique_doc_ids.add(doc_id)

        doc_id_to_name = {}
        if unique_doc_ids:
            doc_result = await db.execute(
                select(Document, KnowledgeBase.name.label("kb_name"))
                .outerjoin(KnowledgeBase, Document.kb_id == KnowledgeBase.id)
                .filter(Document.id.in_([uuid.UUID(did) for did in unique_doc_ids]))
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
            info = doc_id_to_name.get(doc_id, {})

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
        logger.error(f"搜索失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"搜索失败: {str(e)}")


@router.get("/", response_model=List[DocumentResponse])
async def list_documents(
    kb_id: Optional[str] = Query(None, description="知识库ID，不传则查询所有知识库的文档"),
    status: Optional[str] = Query(None, description="文档状态筛选：active（已上架）、inactive（已下架）"),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        logger.info(f"list_documents called with kb_id: {kb_id}, status: {status}")

        query = select(Document).options(
            joinedload(Document.knowledge_base),
            joinedload(Document.category)
        ).filter(Document.owner_id == current_user.user_id)

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
        logger.error(f"Error in list_documents: {type(e).__name__} - {str(e)}", exc_info=True)
        raise


@router.put("/{doc_id}/status")
async def update_status(doc_id: str, status: str, db: AsyncSession = Depends(get_db)):
    try:
        result = await db.execute(select(Document).filter(Document.id == uuid.UUID(doc_id)))
        doc = result.scalar_one_or_none()
        if not doc:
            raise HTTPException(status_code=404, detail="文档不存在")

        if status not in ["draft", "published", "archived"]:
            raise HTTPException(status_code=400, detail="无效的状态值")

        doc.status = status
        await db.commit()
        return {"message": "状态更新成功"}
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档ID")


@router.get("/{doc_id}/preview")
async def preview_doc(doc_id: str, page: int = 1, page_size: int = 500, db: AsyncSession = Depends(get_db)):
    try:
        result = await db.execute(select(Document).filter(Document.id == uuid.UUID(doc_id)))
        doc = result.scalar_one_or_none()
        if not doc:
            raise HTTPException(status_code=404, detail="文档不存在")

        if page < 1:
            page = 1
        if page_size < 10 or page_size > 2000:
            page_size = 500

        result = preview_document(doc.file_path, page=page, page_size=page_size)
        return {
            "doc_id": doc_id,
            "filename": doc.filename,
            **result
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"预览文档失败: {str(e)}")


@router.get("/{doc_id}/chunks")
async def get_doc_chunks(doc_id: str, db: AsyncSession = Depends(get_db)):
    try:
        result = await db.execute(select(Document).filter(Document.id == uuid.UUID(doc_id)))
        doc = result.scalar_one_or_none()
        if not doc:
            raise HTTPException(status_code=404, detail="文档不存在")

        chunks = get_document_chunks(doc.file_path)
        return {
            "doc_id": doc_id,
            "filename": doc.filename,
            "total_chunks": len(chunks),
            "chunks": chunks
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取文本块失败: {str(e)}")


@router.get("/{doc_id}/source/{chunk_index}", response_model=DocumentSourceResponse)
async def get_document_source(
    doc_id: str,
    chunk_index: int = Path(..., ge=0, description="文本块索引"),
    context_chunks: int = Query(1, ge=0, le=5, description="前后上下文块数"),
    db: AsyncSession = Depends(get_db)
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
    try:
        result = await db.execute(select(Document).filter(Document.id == uuid.UUID(doc_id)))
        doc = result.scalar_one_or_none()
        if not doc:
            raise HTTPException(status_code=404, detail="文档不存在")

        chunks = get_document_chunks(doc.file_path)
        
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
        logger.error(f"获取文档来源失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取文档来源失败: {str(e)}")


@router.post("/{doc_id}/reprocess")
async def reprocess_document(
    doc_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
):
    """
    重新处理文档（异步处理）
    
    当文档处理失败时，允许重新处理
    
    Args:
        doc_id: 文档ID
        
    Returns:
        dict: 处理任务信息
    """
    try:
        result = await db.execute(select(Document).filter(Document.id == uuid.UUID(doc_id)))
        doc = result.scalar_one_or_none()
        if not doc:
            raise HTTPException(status_code=404, detail="文档不存在")
        
        if not doc.file_path:
            raise HTTPException(status_code=400, detail="文档文件路径不存在")
        
        kb_id = str(doc.kb_id) if doc.kb_id else ""
        task_upload_id = f"reprocess_{doc_id}_{uuid.uuid4().hex[:8]}"

        # 先删除该文档在向量数据库中的旧向量，避免重复数据
        try:
            vector_store = await VectorStoreManager.get_instance()
            await vector_store.delete_by_document_id(doc_id)
            await vector_store.save_vector_store()
            logger.info(f"已删除文档旧向量: {doc_id}")
        except Exception as e:
            logger.warning(f"删除旧向量失败（可能不存在）: {doc_id}, 错误: {str(e)}")

        # 更新文档状态为处理中
        doc.processing_status = "processing"
        doc.processing_message = "正在重新处理文档..."
        doc.processing_progress = 5
        await db.commit()

        # 创建进度记录
        create_upload_progress(task_upload_id, doc.filename, doc.size)
        update_upload_progress(task_upload_id, status="processing", message="正在重新处理文档...")

        # 将文档处理添加到后台任务
        background_tasks.add_task(
            process_document_async,
            doc_id,
            doc.file_path,
            kb_id,
            task_upload_id,
            None,
            None
        )
        
        # 通知文档列表即将更新
        await notify_doc_list_changed(kb_id, doc_id, "updated")
        
        return {
            "id": doc_id,
            "message": "文档重新处理任务已提交",
            "upload_id": task_upload_id,
            "status": "processing"
        }
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档ID")
    except Exception as e:
        logger.error(f"重新处理文档失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"重新处理文档失败: {str(e)}")


@router.post("/{doc_id}/classify", response_model=DocumentClassificationResponse)
async def classify_document(doc_id: str, db: AsyncSession = Depends(get_db)):
    """
    自动分类文档
    
    Args:
        doc_id: 文档ID
        
    Returns:
        DocumentClassificationResponse: 文档分类结果
    """
    try:
        result = await db.execute(select(Document).filter(Document.id == uuid.UUID(doc_id)))
        doc = result.scalar_one_or_none()
        if not doc:
            raise HTTPException(status_code=404, detail="文档不存在")

        chunks = get_document_chunks(doc.file_path)
        content = "\n\n".join(ch["content"] for ch in chunks)
        
        if len(content) > 2000:
            content = content[:2000]
        
        document_type, topics, domain = DocumentAnalyzer.analyze_document_content(content)
        
        type_labels = {
            "technical": "技术文档",
            "business": "业务文档",
            "report": "报告文档",
            "manual": "操作手册",
            "policy": "政策文件",
            "news": "新闻资讯",
            "other": "其他文档"
        }
        
        domain_labels = {
            "it": "信息技术",
            "finance": "金融",
            "healthcare": "医疗健康",
            "education": "教育",
            "legal": "法律",
            "government": "政府",
            "enterprise": "企业管理",
            "other": "其他领域"
        }

        return DocumentClassificationResponse(
            document_type=document_type,
            document_type_label=type_labels.get(document_type, "其他文档"),
            topics=topics,
            domain=domain,
            domain_label=domain_labels.get(domain, "其他领域"),
            confidence=0.85,
            summary=f"该文档被分类为{type_labels.get(document_type, '其他文档')}，属于{domain_labels.get(domain, '其他领域')}领域"
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"文档分类失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"文档分类失败: {str(e)}")


@router.post("/{doc_id}/quality", response_model=DocumentQualityResponse)
async def evaluate_document_quality(doc_id: str, db: AsyncSession = Depends(get_db)):
    """
    评估文档质量
    
    Args:
        doc_id: 文档ID
        
    Returns:
        DocumentQualityResponse: 文档质量评估结果
    """
    try:
        result = await db.execute(select(Document).filter(Document.id == uuid.UUID(doc_id)))
        doc = result.scalar_one_or_none()
        if not doc:
            raise HTTPException(status_code=404, detail="文档不存在")

        chunks = get_document_chunks(doc.file_path)
        content = "\n\n".join(ch["content"] for ch in chunks)
        
        result = DocumentAnalyzer.evaluate_quality(content)
        
        return DocumentQualityResponse(**result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"文档质量评估失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"文档质量评估失败: {str(e)}")


@router.get("/{doc_id}")
async def get_document(
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        result = await db.execute(select(Document).filter(Document.id == uuid.UUID(doc_id)))
        doc = result.scalar_one_or_none()
        if not doc:
            raise HTTPException(status_code=404, detail="文档不存在")
        require_owner(doc.owner_id, current_user)
        return doc
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档ID")


@router.put("/{doc_id}")
async def update_document(
    doc_id: str,
    data: DocumentUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        result = await db.execute(select(Document).filter(Document.id == uuid.UUID(doc_id)))
        doc = result.scalar_one_or_none()
        if not doc:
            raise HTTPException(status_code=404, detail="文档不存在")
        require_owner(doc.owner_id, current_user)

        if data.category_id:
            doc.category_id = uuid.UUID(data.category_id)
        if data.tags is not None:
            doc.tags = data.tags
        if data.status:
            doc.status = data.status
        if data.kb_id:
            try:
                doc.kb_id = uuid.UUID(data.kb_id)
            except ValueError:
                raise HTTPException(status_code=400, detail="无效的知识库ID")

        await db.commit()
        await db.refresh(doc)
        return {"message": "更新成功"}
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的ID格式")


@router.delete("/{doc_id}")
async def delete_document(
    doc_id: str,
    background_tasks: BackgroundTasks = None,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    删除文档（异步处理）

    此接口会：
    1. 立即返回确认消息
    2. 在后台异步删除文档
    3. 通过WebSocket实时推送删除进度
    """
    try:
        result = await db.execute(select(Document).filter(Document.id == uuid.UUID(doc_id)))
        doc = result.scalar_one_or_none()
        if not doc:
            raise HTTPException(status_code=404, detail="文档不存在")
        require_owner(doc.owner_id, current_user)

        kb_id = str(doc.kb_id)
        file_path = doc.file_path
        task_id = f"delete_{doc_id}_{uuid.uuid4().hex[:8]}"

        # 创建进度记录
        create_upload_progress(task_id, doc.filename, 0)
        update_upload_progress(task_id, status="processing", message="正在删除...")

        # 立即返回，后台删除
        background_tasks.add_task(
            process_document_delete_async,
            doc_id,
            kb_id,
            file_path,
            task_id
        )

        # 通知文档列表即将更新
        await notify_doc_list_changed(kb_id, doc_id, "deleting")

        return {
            "message": "删除任务已提交",
            "doc_id": doc_id,
            "task_id": task_id,
            "status": "processing"
        }
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档ID")


@router.post("/batch/delete")
async def batch_delete(
    body: dict = Body(...),
    background_tasks: BackgroundTasks = None,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    批量删除文档（异步处理）

    此接口会：
    1. 立即返回确认消息
    2. 在后台异步删除文档
    3. 通过WebSocket实时推送删除进度
    """
    ids = body.get("ids", [])
    deleted_docs = []
    deleted_count = 0
    task_ids = []

    for doc_id in ids:
        try:
            result = await db.execute(select(Document).filter(Document.id == uuid.UUID(doc_id)))
            doc = result.scalar_one_or_none()
            if doc and doc.owner_id == current_user.user_id:
                kb_id = str(doc.kb_id)
                file_path = doc.file_path
                task_id = f"delete_{doc_id}_{uuid.uuid4().hex[:8]}"
                task_ids.append(task_id)

                # 创建进度记录
                create_upload_progress(task_id, doc.filename, 0)
                update_upload_progress(task_id, status="processing", message="正在删除...")

                # 立即返回，后台删除
                background_tasks.add_task(
                    process_document_delete_async,
                    doc_id,
                    kb_id,
                    file_path,
                    task_id
                )

                deleted_docs.append({"doc_id": doc_id, "kb_id": kb_id, "task_id": task_id})
                deleted_count += 1

                # 通知文档列表即将更新
                await notify_doc_list_changed(kb_id, doc_id, "deleting")

        except ValueError:
            continue

    return {
        "message": f"已提交{deleted_count}个删除任务",
        "deleted_count": deleted_count,
        "tasks": deleted_docs
    }


@router.get("/upload/progress/{upload_id}")
def get_upload_progress_endpoint(upload_id: str):
    """获取上传进度（轮询方式）"""
    progress = get_upload_progress(upload_id)
    if not progress:
        raise HTTPException(status_code=404, detail="上传任务不存在")
    return progress.to_dict()


@router.websocket("/upload/progress/ws/{upload_id}")
async def upload_progress_ws(websocket: WebSocket, upload_id: str):
    """WebSocket 实时推送上传进度"""
    await websocket.accept()
    
    # 注册 WebSocket 连接
    register_ws_connection(upload_id, websocket)
    
    try:
        # 首先发送当前进度（如果存在）
        progress = get_upload_progress(upload_id)
        if progress:
            await websocket.send_json(progress.to_dict())
        
        # 保持连接
        while True:
            # 定期发送心跳或等待消息
            data = await websocket.receive_json()
            if data.get("action") == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        # 连接断开，注销
        unregister_ws_connection(upload_id, websocket)
    except Exception as e:
        logger.error(f"WebSocket error: {str(e)}")
        unregister_ws_connection(upload_id, websocket)


class DuplicateDetectionResult(BaseModel):
    """重复检测结果模型"""
    document_id: str
    filename: str
    similarity: float
    kb_id: str
    kb_name: Optional[str] = None


@router.post("/duplicate-detect", response_model=List[DuplicateDetectionResult])
async def detect_duplicates(
    request: DuplicateDetectionRequest,
    db: AsyncSession = Depends(get_db)
):
    """
    检测重复或相似文档
    
    Args:
        request: 重复检测请求（包含文档ID或内容、知识库ID、相似度阈值）
        
    Returns:
        List[DuplicateDetectionResult]: 相似文档列表
    """
    try:
        if not request.doc_id and not request.content:
            raise HTTPException(status_code=400, detail="必须提供doc_id或content")
        
        target_content = ""
        
        if request.doc_id:
            result = await db.execute(select(Document).filter(Document.id == uuid.UUID(request.doc_id)))
            doc = result.scalar_one_or_none()
            if not doc:
                raise HTTPException(status_code=404, detail="目标文档不存在")
            chunks = get_document_chunks(doc.file_path)
            target_content = "\n\n".join(ch["content"] for ch in chunks)
        else:
            target_content = request.content or ""
        
        if not target_content.strip():
            raise HTTPException(status_code=400, detail="文档内容不能为空")

        # 词频余弦相似度的默认阈值为 0.7（与 knowledge_base 端点的语义向量相似度 0.85 不同）
        threshold = request.threshold if request.threshold is not None else 0.7
        
        query = select(Document)
        if request.kb_id:
            query = query.filter(Document.kb_id == uuid.UUID(request.kb_id))
        if request.doc_id:
            query = query.filter(Document.id != uuid.UUID(request.doc_id))
        
        result = await db.execute(query)
        candidates = result.scalars().all()
        
        results = []
        for candidate in candidates:
            try:
                chunks = get_document_chunks(candidate.file_path)
                candidate_content = "\n\n".join(ch["content"] for ch in chunks)
                
                similarity = calculate_similarity(target_content, candidate_content)
                
                if similarity >= threshold:
                    kb_name = None
                    if candidate.kb_id:
                        kb_result = await db.execute(select(KnowledgeBase).filter(KnowledgeBase.id == candidate.kb_id))
                        kb = kb_result.scalar_one_or_none()
                        kb_name = kb.name if kb else None
                    
                    results.append(DuplicateDetectionResult(
                        document_id=str(candidate.id),
                        filename=candidate.filename,
                        similarity=round(similarity, 2),
                        kb_id=str(candidate.kb_id) if candidate.kb_id else "",
                        kb_name=kb_name
                    ))
            except Exception:
                continue
        
        results.sort(key=lambda x: x.similarity, reverse=True)
        
        return results[:20]
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"重复检测失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"重复检测失败: {str(e)}")


async def classify_document_with_llm(content: str) -> dict:
    """
    使用LLM对文档进行智能分类（委托给 DocumentAnalyzer）

    Args:
        content: 文档内容

    Returns:
        dict: 分类结果
    """
    vector_store = await VectorStoreManager.get_instance()
    rag_chain = await RAGChain.get_instance(vector_store)
    result = await rag_chain.classify_document(content)
    # DocumentAnalyzer 在解析失败或异常时返回兜底结果；保持原行为：失败时返回 None 以保留规则分析结果
    if result.get("summary") in ("无法解析文档内容", "分类失败"):
        return None
    return result


async def evaluate_quality_with_llm(content: str) -> dict:
    """
    使用LLM评估文档质量（委托给 DocumentAnalyzer）

    Args:
        content: 文档内容

    Returns:
        dict: 质量评估结果
    """
    vector_store = await VectorStoreManager.get_instance()
    rag_chain = await RAGChain.get_instance(vector_store)
    result = await rag_chain.evaluate_document_quality(content)
    summary = result.get("summary")
    # DocumentAnalyzer 在解析失败或异常时返回兜底结果；保持原行为：失败时返回 None 以保留规则分析结果
    if summary in ("无法解析评估结果", "评估失败"):
        return None
    # DocumentAnalyzer 的提示词未要求 overall_grade，按原逻辑从分数推导等级
    overall_score = float(result.get("overall_score", 0))
    if not result.get("overall_grade"):
        if overall_score >= 90:
            result["overall_grade"] = "优秀"
        elif overall_score >= 80:
            result["overall_grade"] = "良好"
        elif overall_score >= 70:
            result["overall_grade"] = "中等"
        elif overall_score >= 60:
            result["overall_grade"] = "及格"
        else:
            result["overall_grade"] = "需改进"
    return result


def calculate_similarity(text1: str, text2: str) -> float:
    """
    计算两个文本的相似度（基于词频余弦相似度 + 字符 Jaccard）

    注意：这是 document.py 重复检测端点使用的轻量规则版实现（词频/字符级）。
    与 DocumentAnalyzer.detect_duplicates 中的语义向量相似度（sentence_transformer
    嵌入余弦）实现不同，二者用途互补，故保留在此处。

    Args:
        text1: 文本1
        text2: 文本2

    Returns:
        float: 相似度（0-1）
    """
    text1 = text1.lower()
    text2 = text2.lower()

    # 分词并统计词频
    words1 = Counter(re.findall(r'\w+', text1))
    words2 = Counter(re.findall(r'\w+', text2))

    if not words1 or not words2:
        return 0.0

    # 计算余弦相似度
    intersection = set(words1.keys()) & set(words2.keys())
    if not intersection:
        cosine = 0.0
    else:
        dot_product = sum(words1[w] * words2[w] for w in intersection)
        magnitude1 = math.sqrt(sum(v ** 2 for v in words1.values()))
        magnitude2 = math.sqrt(sum(v ** 2 for v in words2.values()))
        cosine = dot_product / (magnitude1 * magnitude2) if magnitude1 and magnitude2 else 0.0

    # 字符级 Jaccard 作为补充
    chars1 = set(text1)
    chars2 = set(text2)
    char_jaccard = len(chars1 & chars2) / len(chars1 | chars2) if chars1 | chars2 else 0.0

    return (cosine + char_jaccard) / 2