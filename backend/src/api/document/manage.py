"""文档维护路由：状态、重处理、分类/质量、更新、删除、查重。"""

import asyncio
import logging
import uuid
from typing import List, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import CurrentUser, get_current_user
from src.config import settings
from src.database import get_db
from src.models import Category, Document, KnowledgeBase
from src.schemas.document import (
    DocumentClassificationResponse,
    DocumentQualityResponse,
    DuplicateDetectionRequest,
)
from src.services.document_analyzer import DocumentAnalyzer
from src.services.document_processor import get_document_chunks
from src.services.document_tasks import (
    DOMAIN_LABELS,
    apply_document_status,
    TYPE_LABELS,
    calculate_similarity,
    delete_document_vectors,
    doc_has_indexed_vectors,
    invalidate_doc_cache,
    process_document_async,
    process_document_delete_async,
    schedule_document_reindex,
)
from src.services.notification_service import notify_doc_list_changed
from src.services.progress_manager import create_upload_progress, update_upload_progress
from src.services.vector_store import VectorStoreManager
from src.utils.validators import validate_kb_ownership

from .common import (
    BatchDeleteRequest,
    DocumentUpdate,
    DuplicateDetectionResult,
    get_owned_document as _get_owned_document,
    serialize_document,
)

logger = logging.getLogger("rag_system")

router = APIRouter(tags=["documents"])


@router.put("/{doc_id}/status")
async def update_status(
    doc_id: str,
    # W6 #28：Literal 限制合法值（非法值 422），与 Document.status 枚举对齐
    status: Literal["draft", "published", "archived"],
    background_tasks: BackgroundTasks = None,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    doc = await _get_owned_document(db, doc_id, current_user)
    await apply_document_status(db, doc, status, background_tasks)
    return {"message": "状态更新成功"}


@router.post("/{doc_id}/reprocess")
async def reprocess_document(
    doc_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user)
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
        doc = await _get_owned_document(db, doc_id, current_user)

        if not doc.file_path:
            raise HTTPException(status_code=400, detail="文档文件路径不存在")
        
        kb_id = str(doc.kb_id) if doc.kb_id else ""
        task_upload_id = f"reprocess_{doc_id}_{uuid.uuid4().hex[:8]}"

        # 先删除该文档在向量数据库中的旧向量，避免重复数据
        try:
            vector_store = await VectorStoreManager.get_instance()
            await vector_store.delete_by_document_id(doc_id)
            logger.info(f"已删除文档旧向量: {doc_id}")
        except Exception as e:
            logger.warning(f"删除旧向量失败（可能不存在）: {doc_id}, 错误: {str(e)}")

        # 更新文档状态为处理中
        doc.processing_status = "processing"
        doc.processing_message = "正在重新处理文档..."
        doc.processing_progress = 5
        await db.commit()

        # 创建进度记录
        create_upload_progress(task_upload_id, doc.filename, doc.size, owner_id=current_user.user_id)
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
        await notify_doc_list_changed(
            kb_id, doc_id, "updated", owner_id=getattr(doc, "owner_id", None)
        )
        
        return {
            "id": doc_id,
            "message": "文档重新处理任务已提交",
            "upload_id": task_upload_id,
            "status": "processing"
        }
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的文档ID")
    except HTTPException:
        # _get_owned_document 抛 400/404/403，必须原样上抛，不能被下面的
        # except Exception 吞成 500（否则越权/不存在语义丢失，前端无法区分）
        raise
    except Exception as e:
        logger.error(f"重新处理文档失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="重新处理文档失败，请稍后重试")


@router.post("/{doc_id}/classify", response_model=DocumentClassificationResponse)
async def classify_document(
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user)
):
    """
    自动分类文档

    Args:
        doc_id: 文档ID

    Returns:
        DocumentClassificationResponse: 文档分类结果
    """
    try:
        doc = await _get_owned_document(db, doc_id, current_user)

        chunks = await asyncio.to_thread(get_document_chunks, doc.file_path)
        content = "\n\n".join(ch["content"] for ch in chunks)

        if len(content) > 2000:
            content = content[:2000]

        # 同步分析（含 CPU 密集统计），移入线程池
        document_type, topics, domain = await asyncio.to_thread(
            DocumentAnalyzer.analyze_document_content, content
        )

        return DocumentClassificationResponse(
            document_type=document_type,
            document_type_label=TYPE_LABELS.get(document_type, "其他文档"),
            topics=topics,
            domain=domain,
            domain_label=DOMAIN_LABELS.get(domain, "其他领域"),
            confidence=0.85,
            summary=f"该文档被分类为{TYPE_LABELS.get(document_type, '其他文档')}，属于{DOMAIN_LABELS.get(domain, '其他领域')}领域"
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        # _get_owned_document 抛 400/404/403，原样上抛，避免被吞成 500
        raise
    except Exception as e:
        logger.error(f"文档分类失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="文档分类失败，请稍后重试")


@router.post("/{doc_id}/quality", response_model=DocumentQualityResponse)
async def evaluate_document_quality(
    doc_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user)
):
    """
    评估文档质量

    Args:
        doc_id: 文档ID

    Returns:
        DocumentQualityResponse: 文档质量评估结果
    """
    try:
        doc = await _get_owned_document(db, doc_id, current_user)

        chunks = await asyncio.to_thread(get_document_chunks, doc.file_path)
        content = "\n\n".join(ch["content"] for ch in chunks)

        result = await asyncio.to_thread(DocumentAnalyzer.evaluate_quality, content)
        
        return DocumentQualityResponse(**result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        # _get_owned_document 抛 400/404/403，原样上抛，避免被吞成 500
        raise
    except Exception as e:
        logger.error(f"文档质量评估失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="文档质量评估失败，请稍后重试")


@router.put("/{doc_id}")
async def update_document(
    doc_id: str,
    data: DocumentUpdate,
    background_tasks: BackgroundTasks = None,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        doc = await _get_owned_document(db, doc_id, current_user)
        old_kb_id = doc.kb_id
        new_kb_id = doc.kb_id
        kb_moved = False

        if data.category_id:
            # 外键校验：Category 为全局资源（无归属字段），仅校验存在性
            try:
                category_uuid = uuid.UUID(data.category_id)
            except ValueError:
                raise HTTPException(status_code=400, detail="无效的分类ID")
            category = (
                await db.execute(select(Category).filter(Category.id == category_uuid))
            ).scalar_one_or_none()
            if not category:
                raise HTTPException(status_code=404, detail="分类不存在")
            doc.category_id = category_uuid
        if data.tags is not None:
            doc.tags = data.tags

        if data.kb_id:
            # 外键校验：kb_id 必须存在且属于当前用户，防止越权移动文档
            try:
                kb_uuid = uuid.UUID(data.kb_id)
            except ValueError:
                raise HTTPException(status_code=400, detail="无效的知识库ID")
            await validate_kb_ownership(db, [data.kb_id], current_user)
            new_kb_id = kb_uuid
            kb_moved = kb_uuid != old_kb_id

        if kb_moved and getattr(doc, "processing_status", None) == "processing":
            raise HTTPException(status_code=409, detail="文档正在处理中，请稍后再试")

        vectors_deleted = False
        # 移动 KB 前先清理旧 KB 向量（失败则拒绝整个更新，避免双库残留）。
        if kb_moved and doc_has_indexed_vectors(doc):
            await delete_document_vectors(str(doc.id))
            vectors_deleted = True
        if kb_moved:
            doc.kb_id = new_kb_id
            doc.chunks_count = 0

        new_status = data.status or doc.status
        if new_status != doc.status and getattr(doc, "processing_status", None) == "processing":
            raise HTTPException(status_code=409, detail="文档正在处理中，请稍后再试")
        if new_status != doc.status:
            if new_status != "published":
                if doc_has_indexed_vectors(doc) and not vectors_deleted:
                    await delete_document_vectors(str(doc.id))
                    vectors_deleted = True
                doc.status = new_status
                doc.chunks_count = 0
                await db.commit()
                invalidate_doc_cache(
                    str(old_kb_id) if old_kb_id else None,
                    str(new_kb_id) if new_kb_id else None,
                )
                return {"message": "更新成功"}

            if doc_has_indexed_vectors(doc) and not vectors_deleted:
                await delete_document_vectors(str(doc.id))
                vectors_deleted = True
            doc.processing_status = "processing"
            doc.processing_message = "正在重建索引..."
            doc.processing_progress = 0
            doc.chunks_count = 0
            await db.commit()
            schedule_document_reindex(doc, background_tasks)
            invalidate_doc_cache(
                str(old_kb_id) if old_kb_id else None,
                str(new_kb_id) if new_kb_id else None,
            )
            return {"message": "更新成功，索引重建中"}

        # 仅移动 KB 且原状态为 published：需要在新的 kb 下重建索引。
        if kb_moved and doc.status == "published":
            doc.processing_status = "processing"
            doc.processing_message = "正在重建索引..."
            doc.processing_progress = 0
            doc.chunks_count = 0
            await db.commit()
            schedule_document_reindex(doc, background_tasks)
            invalidate_doc_cache(
                str(old_kb_id) if old_kb_id else None,
                str(new_kb_id) if new_kb_id else None,
            )
            return {"message": "更新成功，索引重建中"}

        await db.commit()
        if kb_moved:
            invalidate_doc_cache(
                str(old_kb_id) if old_kb_id else None,
                str(new_kb_id) if new_kb_id else None,
            )
        return {"message": "更新成功"}
    except HTTPException:
        raise
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
        doc = await _get_owned_document(db, doc_id, current_user)

        kb_id = str(doc.kb_id)
        file_path = doc.file_path
        task_id = f"delete_{doc_id}_{uuid.uuid4().hex[:8]}"

        # 创建进度记录
        create_upload_progress(task_id, doc.filename, 0, owner_id=current_user.user_id)
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
        await notify_doc_list_changed(
            kb_id, doc_id, "deleting", owner_id=getattr(doc, "owner_id", None)
        )

        # P2-4 审计：文档删除请求（best-effort）
        from src.services.audit_service import record_audit
        await record_audit(
            action="document.delete",
            resource_type="document",
            resource_id=doc_id,
            detail={"filename": getattr(doc, "filename", None)},
            actor_user_id=current_user.user_id,
        )

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
    body: BatchDeleteRequest = Body(...),
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
    ids = body.ids
    max_ids = int(settings.security.BATCH_DELETE_MAX_IDS)
    if max_ids and len(ids) > max_ids:
        raise HTTPException(
            status_code=400,
            detail=f"单次批量删除最多允许 {max_ids} 个文档，当前 {len(ids)} 个",
        )
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
                create_upload_progress(task_id, doc.filename, 0, owner_id=current_user.user_id)
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
                await notify_doc_list_changed(
                    kb_id, doc_id, "deleting", owner_id=current_user.user_id
                )

        except ValueError:
            continue

    return {
        "message": f"已提交{deleted_count}个删除任务",
        "deleted_count": deleted_count,
        "tasks": deleted_docs
    }


@router.post("/duplicate-detect", response_model=List[DuplicateDetectionResult])
async def detect_duplicates(
    request: DuplicateDetectionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user)
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
            doc = await _get_owned_document(db, request.doc_id, current_user)
            chunks = await asyncio.to_thread(get_document_chunks, doc.file_path)
            target_content = "\n\n".join(ch["content"] for ch in chunks)
        else:
            target_content = request.content or ""

        if not target_content.strip():
            raise HTTPException(status_code=400, detail="文档内容不能为空")

        # 词频余弦相似度的默认阈值为 0.7（与 knowledge_base 端点的语义向量相似度 0.85 不同）
        threshold = request.threshold if request.threshold is not None else 0.7

        # 仅在当前用户的文档范围内查重
        query = select(Document).filter(Document.owner_id == current_user.user_id)
        if request.kb_id:
            query = query.filter(Document.kb_id == uuid.UUID(request.kb_id))
        if request.doc_id:
            query = query.filter(Document.id != uuid.UUID(request.doc_id))
        
        result = await db.execute(query)
        candidates = result.scalars().all()

        # 候选数上限：每个候选都要下载+解析文档（同步阻塞），
        # 不设上限时大库会放大为长时间占用线程池
        max_candidates = 20
        if len(candidates) > max_candidates:
            logger.warning(
                f"重复检测候选数 {len(candidates)} 超过上限 {max_candidates}，仅处理前 {max_candidates} 个"
            )
            candidates = candidates[:max_candidates]

        results = []
        for candidate in candidates:
            try:
                chunks = await asyncio.to_thread(get_document_chunks, candidate.file_path)
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
    except HTTPException:
        # _get_owned_document 抛 400/404/403，原样上抛，避免被吞成 500
        raise
    except Exception as e:
        logger.error(f"重复检测失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="重复检测失败，请稍后重试")
