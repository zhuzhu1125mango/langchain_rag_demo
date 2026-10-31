"""知识库维护路由：创建/更新/删除/批量删除/设为默认（CODE-1 拆分）。"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete
from typing import List
from datetime import timedelta
import uuid
import asyncio
import logging
from src.database import get_db
from src.auth import get_current_user, CurrentUser, require_owner
from src.models import KnowledgeBase, Document
from src.services.vector_store import VectorStoreManager
from src.services.semantic_cache_service import schedule_invalidation as _schedule_semantic_cache_invalidation
from src.utils.error_handling import ErrorSeverity, log_exception

logger = logging.getLogger("knowledge_base_api")

KB_LIST_CACHE_PREFIX = "kb:list"
KB_LIST_CACHE_TTL = timedelta(seconds=60)
BATCH_DELETE_MAX_IDS = 1000
BATCH_DELETE_CHUNK_SIZE = 800

# 缓存操作兜底超时（秒）。即使 Redis 客户端自身已配置超时，
# 这里再加一层保护，确保任何情况下知识库列表接口都不会被缓存阻塞。
KB_CACHE_OPERATION_TIMEOUT = 2.0


async def _count_kb_residual(vector_store, kb_id: str) -> int:
    """删除后核对某知识库的残留切片数；无法核对时按 0 处理（不制造假告警）。

    主要用于发现「delete 返回成功但实际 0 影响」的形态：历史切片的 kb_id 若为
    空串（schema 升级后补字段时的默认值），按库过滤永远匹配不到。
    """
    milvus = getattr(vector_store, "milvus_service", None)
    count_by_kb = getattr(milvus, "count_by_kb", None)
    if count_by_kb is None:
        return 0
    try:
        return int(await count_by_kb(kb_id) or 0)
    except Exception as exc:
        log_exception(
            logger, exc,
            action="knowledge_base.delete.vectors_verify",
            severity=ErrorSeverity.RECOVERABLE,
            message="无法核对向量残留数量，跳过删除后校验",
            kb_id=kb_id,
        )
        return 0


from .common import (
    BatchDeleteKnowledgeBasesRequest,
    BatchDeleteKnowledgeBasesResponse,
    KnowledgeBaseCreate,
    KnowledgeBaseResponse,
    KnowledgeBaseUpdate,
    invalidate_kb_list_cache,
    set_single_default,
)

router = APIRouter(tags=["knowledge_bases"])

@router.post("/", response_model=KnowledgeBaseResponse)
async def create_knowledge_base(
    data: KnowledgeBaseCreate,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    result = await db.execute(
        select(KnowledgeBase).filter(
            KnowledgeBase.name == data.name,
            KnowledgeBase.owner_id == current_user.user_id,
        )
    )
    existing_kb = result.scalar_one_or_none()
    if existing_kb:
        raise HTTPException(status_code=400, detail="知识库名称已存在")

    count_result = await db.execute(
        select(func.count(KnowledgeBase.id)).filter(KnowledgeBase.owner_id == current_user.user_id)
    )
    is_first = count_result.scalar_one() == 0

    kb = KnowledgeBase(
        id=uuid.uuid4(),
        name=data.name,
        description=data.description,
        embedding_model=data.embedding_model,
        is_default=is_first,
        owner_id=current_user.user_id,
    )
    db.add(kb)
    await db.commit()
    await db.refresh(kb)

    await invalidate_kb_list_cache(current_user.user_id)

    # P2-4 审计：知识库创建（best-effort）
    from src.services.audit_service import record_audit
    await record_audit(
        action="kb.create",
        resource_type="knowledge_base",
        resource_id=str(kb.id),
        detail={"name": kb.name},
        actor_user_id=current_user.user_id,
    )

    return KnowledgeBaseResponse(
        id=str(kb.id),
        name=kb.name,
        description=kb.description,
        embedding_model=kb.embedding_model,
        is_default=kb.is_default,
        status=kb.status,
        document_count=0,
        created_at=kb.created_at.isoformat(),
        updated_at=kb.updated_at.isoformat() if kb.updated_at else kb.created_at.isoformat()
    )

@router.put("/{kb_id}", response_model=KnowledgeBaseResponse)
async def update_knowledge_base(
    kb_id: str,
    data: KnowledgeBaseUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        kb_id_uuid = uuid.UUID(kb_id)

        result = await db.execute(select(KnowledgeBase).filter(KnowledgeBase.id == kb_id_uuid))
        kb = result.scalar_one_or_none()
        if not kb:
            raise HTTPException(status_code=404, detail="知识库不存在")
        require_owner(kb.owner_id, current_user)

        if data.name:
            existing_result = await db.execute(select(KnowledgeBase).filter(
                KnowledgeBase.name == data.name,
                KnowledgeBase.id != kb_id_uuid,
                KnowledgeBase.owner_id == current_user.user_id,
            ))
            existing_kb = existing_result.scalar_one_or_none()
            if existing_kb:
                raise HTTPException(status_code=400, detail="知识库名称已存在")
            kb.name = data.name
        
        if data.description is not None:
            kb.description = data.description
        
        if data.embedding_model:
            kb.embedding_model = data.embedding_model
        
        if data.status:
            kb.status = data.status

        await db.commit()
        await db.refresh(kb)

        await invalidate_kb_list_cache(current_user.user_id)

        doc_count_result = await db.execute(
            select(func.count(Document.id)).filter(
                Document.kb_id == kb_id_uuid,
                Document.owner_id == current_user.user_id
            )
        )
        doc_count = doc_count_result.scalar_one()

        return KnowledgeBaseResponse(
            id=str(kb.id),
            name=kb.name,
            description=kb.description,
            embedding_model=kb.embedding_model,
            is_default=kb.is_default,
            status=kb.status,
            document_count=doc_count,
            created_at=kb.created_at.isoformat(),
            updated_at=kb.updated_at.isoformat() if kb.updated_at else kb.created_at.isoformat()
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的知识库ID")

@router.delete("/{kb_id}")
async def delete_knowledge_base(
    kb_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        result = await db.execute(select(KnowledgeBase).filter(KnowledgeBase.id == uuid.UUID(kb_id)))
        kb = result.scalar_one_or_none()
        if not kb:
            raise HTTPException(status_code=404, detail="知识库不存在")
        require_owner(kb.owner_id, current_user)

        if kb.is_default:
            other_result = await db.execute(
                select(KnowledgeBase).filter(
                    KnowledgeBase.id != uuid.UUID(kb_id),
                    KnowledgeBase.owner_id == current_user.user_id,
                )
            )
            other_kbs = other_result.scalars().all()
            if other_kbs:
                other_kbs[0].is_default = True
            else:
                raise HTTPException(status_code=400, detail="不能删除最后一个知识库")

        doc_result = await db.execute(select(Document).filter(Document.kb_id == kb.id))
        docs = doc_result.scalars().all()

        from src.services.minio_service import MinioService

        # 向量清理：按 kb_id 一次删除并统一 flush（替代逐文档删除 + 多次 flush，
        # 避免 Milvus 单集合 0.1 QPS 限流）；失败不阻塞数据库记录删除，仅记录告警。
        try:
            vector_store = await VectorStoreManager.get_instance()
            await vector_store.delete_by_kb_ids([str(kb.id)])
        except Exception as exc:
            # 失败后仍会删 DB 行 —— 这些向量从此没有任何元数据指向，只能靠日志人工清理。
            # 属于永久性数据残留（检索仍可命中已删库内容），不是「等下次重试」的可恢复抖动。
            log_exception(
                logger, exc,
                action="knowledge_base.delete.vectors",
                severity=ErrorSeverity.REPORTABLE,
                message="删除知识库时向量清理失败，DB 记录仍被删除：该库切片成为无主向量"
                        "（检索可命中、无元数据可追溯），需按 kb_id 人工清理",
                kb_id=str(kb.id),
                documents=len(docs),
            )
            # 回退到逐文档删除：部分历史数据的 kb_id 未回填（默认空串），
            # 按库过滤删不到，但按 document_id 删得到。
            doc_ids = [str(d.id) for d in docs]
            if doc_ids:
                try:
                    await vector_store.delete_by_document_ids(doc_ids)
                    logger.info(
                        "按 kb_id 删除向量失败后，回退逐文档删除成功: kb=%s docs=%d",
                        kb.id, len(doc_ids),
                    )
                except Exception as retry_exc:
                    log_exception(
                        logger, retry_exc,
                        action="knowledge_base.delete.vectors_retry",
                        severity=ErrorSeverity.REPORTABLE,
                        message="按知识库与按文档两轮向量清理均失败，确认存在无主向量",
                        kb_id=str(kb.id),
                        documents=len(doc_ids),
                    )
        else:
            # 删除成功也要核对：kb_id 字段对历史数据可能为空串，此时 delete 会
            # 「成功返回 0 影响」而不报错——元数据照删，向量全留，正是最难发现的形态。
            residual = await _count_kb_residual(vector_store, str(kb.id))
            if residual:
                doc_ids = [str(d.id) for d in docs]
                if doc_ids:
                    try:
                        await vector_store.delete_by_document_ids(doc_ids)
                        residual = await _count_kb_residual(vector_store, str(kb.id))
                    except Exception as retry_exc:
                        log_exception(
                            logger, retry_exc,
                            action="knowledge_base.delete.vectors_residual_retry",
                            severity=ErrorSeverity.REPORTABLE,
                            message="发现向量残留后按文档重删失败",
                            kb_id=str(kb.id),
                            residual=residual,
                        )
                if residual:
                    log_exception(
                        logger, RuntimeError("知识库删除后仍有向量残留"),
                        action="knowledge_base.delete.vectors_residual",
                        severity=ErrorSeverity.REPORTABLE,
                        message="按 kb_id 删除返回成功但仍有残留切片：这些切片将可被检索命中，"
                                "且删除 DB 记录后无法再追溯",
                        kb_id=str(kb.id),
                        residual=residual,
                        documents=len(docs),
                    )

        # 删除 MinIO 中的原始文件（并发执行），失败仅告警不阻塞。
        minio_service = await MinioService.get_instance()

        async def cleanup_minio_file(doc: Document) -> None:
            if not doc.file_path or not doc.file_path.startswith("minio://"):
                return
            try:
                await minio_service.delete_file_async(doc.file_path)
            except Exception as exc:
                log_exception(
                    logger, exc,
                    action="knowledge_base.delete.object",
                    severity=ErrorSeverity.RECOVERABLE,
                    message="删除 MinIO 文件失败，继续删除数据库记录",
                    doc_id=str(doc.id),
                )

        await asyncio.gather(*(cleanup_minio_file(doc) for doc in docs))

        # 批量删除数据库记录：先删 Document，再删 KnowledgeBase，防止外键冲突。
        await db.execute(delete(Document).filter(Document.kb_id == kb.id))
        await db.execute(delete(KnowledgeBase).filter(KnowledgeBase.id == kb.id))
        await db.commit()

        await invalidate_kb_list_cache(current_user.user_id)

        # P1-3：知识库内容已清空，失效相关语义缓存
        _schedule_semantic_cache_invalidation(str(kb.id))

        # P2：Wiki 级联清理（wiki_pages 行 + MinIO 正文），失败不阻断删除主流程
        from src.services.wiki_cascade import on_kb_deleted

        await on_kb_deleted(db, str(kb.id))

        return {"message": "知识库已删除"}
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的知识库ID")

async def _cleanup_kb_artifacts(
    doc_file_paths: List[tuple],
    kb_ids: List[str],
) -> None:
    """批量删除知识库的遗留产物清理（W6 #78b 后台任务）。

    - MinIO 原始文件：按 BATCH_DELETE_CHUNK_SIZE 分批并发删除（file_path 为
      请求内快照，DB 行已删除）；
    - Wiki 级联：on_kb_deleted 清理 wiki_pages 行与 MinIO 正文（独立会话）。

    失败仅记录告警，不重试、不抛出——后台清理失败可后续人工清理，
    不影响已完成的删除语义。
    """
    from src.database import async_session_maker
    from src.services.minio_service import MinioService
    from src.services.wiki_cascade import on_kb_deleted

    if doc_file_paths:
        try:
            minio_service = await MinioService.get_instance()
            for i in range(0, len(doc_file_paths), BATCH_DELETE_CHUNK_SIZE):
                batch = doc_file_paths[i:i + BATCH_DELETE_CHUNK_SIZE]
                await asyncio.gather(
                    *(
                        minio_service.delete_file_async(file_path)
                        for _, file_path in batch
                    )
                )
        except Exception as exc:
            log_exception(
                logger, exc,
                action="knowledge_base.batch_delete.objects",
                severity=ErrorSeverity.RECOVERABLE,
                message="批量删除知识库后台 MinIO 清理失败",
                kb_count=len(kb_ids),
            )

    if kb_ids:
        try:
            async with async_session_maker() as db:
                for kb_id in kb_ids:
                    await on_kb_deleted(db, kb_id)
        except Exception as exc:
            log_exception(
                logger, exc,
                action="knowledge_base.batch_delete.wiki_cascade",
                severity=ErrorSeverity.RECOVERABLE,
                message="批量删除知识库后台 Wiki 级联清理失败",
                kb_count=len(kb_ids),
            )

    logger.info(
        "批量删除知识库后台清理完成: MinIO文件=%d KB=%d",
        len(doc_file_paths),
        len(kb_ids),
    )

@router.post("/batch-delete", response_model=BatchDeleteKnowledgeBasesResponse)
async def batch_delete_knowledge_bases(
    request: BatchDeleteKnowledgeBasesRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    批量删除知识库及其关联文档。

    仅删除当前用户拥有的知识库。若待删除列表包含默认知识库，会自动将默认标记
    转移到该用户的其他知识库；若用户选择删除其全部知识库，则返回 400。
    数据库记录与向量清理在请求内完成（毫秒级）；MinIO 原始文件清理与 Wiki
    级联下沉到后台任务（W6 #78b，独立 DB 会话）——上限 1000 KB 的长耗时
    IO 不再阻塞响应。后台清理失败仅告警，不影响删除语义与已返回的计数。
    """
    if not request.ids:
        return BatchDeleteKnowledgeBasesResponse(deleted_count=0, skipped_ids=[])

    if len(request.ids) > BATCH_DELETE_MAX_IDS:
        logger.warning(
            "批量删除知识库被拒绝: ids数量=%d 超过上限=%d",
            len(request.ids),
            BATCH_DELETE_MAX_IDS,
        )
        raise HTTPException(
            status_code=400,
            detail=f"单次批量删除最多允许 {BATCH_DELETE_MAX_IDS} 个知识库",
        )

    try:
        kb_ids = [uuid.UUID(kid) for kid in request.ids]
    except ValueError:
        logger.warning("批量删除知识库被拒绝: 包含无效的知识库ID %s", request.ids)
        raise HTTPException(status_code=400, detail="包含无效的知识库ID")

    result = await db.execute(
        select(KnowledgeBase).filter(
            KnowledgeBase.id.in_(kb_ids),
            KnowledgeBase.owner_id == current_user.user_id,
        )
    )
    kbs = result.scalars().all()
    found_ids = {kb.id for kb in kbs}
    skipped_ids = [kid for kid in request.ids if uuid.UUID(kid) not in found_ids]

    if not kbs:
        return BatchDeleteKnowledgeBasesResponse(deleted_count=0, skipped_ids=skipped_ids)

    default_kb = next((kb for kb in kbs if kb.is_default), None)
    if default_kb:
        result = await db.execute(
            select(KnowledgeBase).filter(
                KnowledgeBase.owner_id == current_user.user_id,
                ~KnowledgeBase.id.in_(found_ids),
            ).order_by(KnowledgeBase.created_at.desc())
        )
        remaining = result.scalars().all()
        if not remaining:
            logger.warning(
                "批量删除知识库被拒绝: 用户 %s 尝试删除其全部知识库",
                current_user.user_id,
            )
            raise HTTPException(status_code=400, detail="不能删除最后一个知识库")
        remaining[0].is_default = True

    # 批量删除向量：合并为一个 delete 表达式并统一 flush 一次，
    # 避免每个知识库单独 flush 触发 Milvus 单集合 0.1 QPS 限流。
    # 向量清理失败不应阻塞数据库记录删除，仅记录告警，避免服务抖动导致知识库无法删除。
    if found_ids:
        try:
            vector_store = await VectorStoreManager.get_instance()
            await vector_store.delete_by_kb_ids([str(kb_id) for kb_id in found_ids])
        except Exception as exc:
            # 批量删除会紧接着清掉 DB 行，这之后没有任何记录能反查出该删哪些向量。
            # 日志必须带上具体 kb_id，否则运维只有「失败」二字，无法人工补救。
            log_exception(
                logger, exc,
                action="knowledge_base.batch_delete.vectors",
                severity=ErrorSeverity.REPORTABLE,
                message="批量删除知识库时向量清理失败，DB 记录仍被删除：这些库的切片成为"
                        "无主向量（检索可命中、无元数据可追溯），需按 kb_ids 人工清理",
                kb_count=len(found_ids),
                kb_ids=",".join(sorted(str(k) for k in found_ids)[:20]),
            )

    # 删除 MinIO 中的原始文件：W6 #78b 下沉到后台任务（独立会话 + 分批并发），
    # 请求内仅快照 file_path（DB 行即将被删）。MinIO 清理失败不影响删除语义。
    doc_result = await db.execute(
        select(Document).filter(Document.kb_id.in_(found_ids))
    )
    docs = doc_result.scalars().all()
    doc_file_paths = [
        (str(doc.id), doc.file_path)
        for doc in docs
        if doc.file_path and doc.file_path.startswith("minio://")
    ]

    # 分批删除数据库记录，避免超大 IN 子句；先删 Document，再删 KnowledgeBase，防止外键冲突。
    found_ids_list = list(found_ids)
    for i in range(0, len(found_ids_list), BATCH_DELETE_CHUNK_SIZE):
        batch = found_ids_list[i:i + BATCH_DELETE_CHUNK_SIZE]
        await db.execute(delete(Document).filter(Document.kb_id.in_(batch)))

    for i in range(0, len(found_ids_list), BATCH_DELETE_CHUNK_SIZE):
        batch = found_ids_list[i:i + BATCH_DELETE_CHUNK_SIZE]
        await db.execute(
            delete(KnowledgeBase).filter(
                KnowledgeBase.id.in_(batch),
                KnowledgeBase.owner_id == current_user.user_id,
            )
        )
    await db.commit()

    await invalidate_kb_list_cache(current_user.user_id)

    # P1-3：知识库内容已清空，失效相关语义缓存
    for kb in kbs:
        _schedule_semantic_cache_invalidation(str(kb.id))

    # P2 + W6 #78b：Wiki 级联清理与 MinIO 文件清理统一为后台任务（独立 DB 会话，
    # 不复用请求级会话——P1-9/P1-10 教训：后台任务不得占用请求连接）
    if doc_file_paths or kbs:
        kb_ids_str = [str(kb.id) for kb in kbs]
        background_tasks.add_task(_cleanup_kb_artifacts, doc_file_paths, kb_ids_str)

    logger.info(
        "批量删除知识库成功: 用户=%s 删除数量=%d 跳过数量=%d",
        current_user.user_id,
        len(kbs),
        len(skipped_ids),
    )

    return BatchDeleteKnowledgeBasesResponse(
        deleted_count=len(kbs),
        skipped_ids=skipped_ids,
    )

@router.post("/{kb_id}/set_default")
async def set_default_knowledge_base(
    kb_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        result = await db.execute(select(KnowledgeBase).filter(KnowledgeBase.id == uuid.UUID(kb_id)))
        kb = result.scalar_one_or_none()
        if not kb:
            raise HTTPException(status_code=404, detail="知识库不存在")
        require_owner(kb.owner_id, current_user)

        await set_single_default(db, uuid.UUID(kb_id), current_user.user_id)

        return {"message": "已设置为默认知识库"}
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的知识库ID")
