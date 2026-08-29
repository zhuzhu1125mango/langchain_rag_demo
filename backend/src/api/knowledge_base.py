"""
知识库管理API - KnowledgeBase API

提供知识库的CRUD操作接口，包括：
1. 创建知识库
2. 获取知识库列表
3. 获取知识库详情
4. 更新知识库
5. 删除知识库
6. 设置默认知识库
7. 智能推荐知识库
8. 生成知识图谱

注：重复文档检测、文档质量评估、文档自动分类已统一由 document 模块提供。
"""

from fastapi import APIRouter, Depends, HTTPException, Body, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func, delete
from pydantic import BaseModel
from typing import List, Optional
from datetime import timedelta
import uuid
import asyncio
import logging
from src.database import get_db
from src.auth import get_current_user, CurrentUser, require_owner
from src.models import KnowledgeBase, Document
from src.services.vector_store import VectorStoreManager
from src.services.cache_service import CacheService
from src.services.rag_chain import RAGChain

logger = logging.getLogger("knowledge_base_api")

KB_LIST_CACHE_PREFIX = "kb:list"
KB_LIST_CACHE_TTL = timedelta(seconds=60)
BATCH_DELETE_MAX_IDS = 1000
BATCH_DELETE_CHUNK_SIZE = 800

# 缓存操作兜底超时（秒）。即使 Redis 客户端自身已配置超时，
# 这里再加一层保护，确保任何情况下知识库列表接口都不会被缓存阻塞。
KB_CACHE_OPERATION_TIMEOUT = 2.0

router = APIRouter(prefix="/knowledge_bases", tags=["knowledge_bases"])


class KnowledgeBaseCreate(BaseModel):
    """创建知识库请求模型。"""
    name: str
    description: Optional[str] = None
    embedding_model: Optional[str] = "bge-m3:latest"


class KnowledgeBaseUpdate(BaseModel):
    """更新知识库请求模型。"""
    name: Optional[str] = None
    description: Optional[str] = None
    embedding_model: Optional[str] = None
    status: Optional[str] = None


class KnowledgeBaseResponse(BaseModel):
    """知识库响应模型。"""
    id: str
    name: str
    description: Optional[str] = None
    embedding_model: str
    is_default: bool
    status: str
    document_count: int
    created_at: str
    updated_at: str


class KBRecommendationRequest(BaseModel):
    """知识库推荐请求模型。"""
    question: str
    top_k: Optional[int] = 3


class KBRecommendationResponse(BaseModel):
    """知识库推荐响应模型。"""
    kb_id: str
    relevance_score: float
    matched_chunks: int


class KnowledgeGraphResponse(BaseModel):
    """知识图谱响应模型。"""
    nodes: List[dict]
    edges: List[dict]
    summary: str


class BatchDeleteKnowledgeBasesRequest(BaseModel):
    """批量删除知识库请求模型。"""
    ids: List[str]


class BatchDeleteKnowledgeBasesResponse(BaseModel):
    """批量删除知识库响应模型。"""
    deleted_count: int
    skipped_ids: List[str]


async def set_single_default(db: AsyncSession, kb_id: uuid.UUID, owner_id: str):
    """设置指定用户的唯一默认知识库。

    「清空默认标志」仅作用于该 owner 名下的知识库，避免跨用户重置。

    Args:
        db: 数据库会话。
        kb_id: 目标知识库 ID（调用方需已校验归属）。
        owner_id: 资源所有者标识。
    """
    await db.execute(
        update(KnowledgeBase)
        .where(KnowledgeBase.owner_id == owner_id)
        .values(is_default=False)
    )
    result = await db.execute(select(KnowledgeBase).filter(KnowledgeBase.id == kb_id))
    kb = result.scalar_one_or_none()
    if kb:
        kb.is_default = True
    await db.commit()


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


class KnowledgeBaseListResponse(BaseModel):
    """知识库列表响应模型。"""
    items: List[KnowledgeBaseResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


def _kb_list_cache_key(user_id: str, page: int, page_size: int) -> str:
    """生成知识库列表缓存键。"""
    return f"{KB_LIST_CACHE_PREFIX}:{user_id}:{page}:{page_size}"


async def invalidate_kb_list_cache(user_id: str) -> None:
    """使指定用户的知识库列表缓存失效。"""
    try:
        cache = await CacheService.get_instance()
        await cache.clear_pattern(f"{KB_LIST_CACHE_PREFIX}:{user_id}:*")
    except Exception as exc:
        logger.warning("清除知识库列表缓存失败: %s", exc)


@router.get("/", response_model=KnowledgeBaseListResponse)
async def list_knowledge_bases(
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(10, ge=1, le=1000, description="每页数量"),
    current_user: CurrentUser = Depends(get_current_user),
):
    cache_key = _kb_list_cache_key(current_user.user_id, page, page_size)
    cache = None
    cached = None
    try:
        cache = await asyncio.wait_for(
            CacheService.get_instance(), timeout=KB_CACHE_OPERATION_TIMEOUT
        )
        cached = await asyncio.wait_for(
            cache.get(cache_key), timeout=KB_CACHE_OPERATION_TIMEOUT
        )
    except Exception as exc:
        logger.warning("读取知识库列表缓存失败: %s", exc)

    if cached is not None:
        return KnowledgeBaseListResponse(**cached)

    subq = select(
        Document.kb_id,
        func.count(Document.id).label("doc_count")
    ).filter(Document.owner_id == current_user.user_id).group_by(Document.kb_id).subquery()

    owner_filter = KnowledgeBase.owner_id == current_user.user_id
    total_result = await db.execute(select(func.count(KnowledgeBase.id)).filter(owner_filter))
    total = total_result.scalar_one()

    offset = (page - 1) * page_size

    result = await db.execute(
        select(
            KnowledgeBase,
            func.coalesce(subq.c.doc_count, 0).label("document_count")
        ).outerjoin(subq, KnowledgeBase.id == subq.c.kb_id)
        .filter(owner_filter)
        .offset(offset)
        .limit(page_size)
    )

    items = []
    for kb, doc_count in result.all():
        items.append(KnowledgeBaseResponse(
            id=str(kb.id),
            name=kb.name,
            description=kb.description,
            embedding_model=kb.embedding_model,
            is_default=kb.is_default,
            status=kb.status,
            document_count=doc_count,
            created_at=kb.created_at.isoformat(),
            updated_at=kb.updated_at.isoformat() if kb.updated_at else kb.created_at.isoformat()
        ))

    total_pages = (total + page_size - 1) // page_size

    response = KnowledgeBaseListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages
    )

    if cache is not None:
        try:
            await asyncio.wait_for(
                cache.set(cache_key, response.model_dump(), expire=KB_LIST_CACHE_TTL),
                timeout=KB_CACHE_OPERATION_TIMEOUT,
            )
        except Exception as exc:
            logger.warning("写入知识库列表缓存失败: %s", exc)

    return response


@router.get("/default", response_model=KnowledgeBaseResponse)
async def get_default_knowledge_base(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    result = await db.execute(
        select(KnowledgeBase).filter(
            KnowledgeBase.is_default == True,
            KnowledgeBase.owner_id == current_user.user_id,
        )
    )
    kb = result.scalar_one_or_none()
    if not kb:
        raise HTTPException(status_code=404, detail="没有找到默认知识库")

    doc_count_result = await db.execute(
        select(func.count(Document.id)).filter(
            Document.kb_id == kb.id,
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


@router.get("/{kb_id}", response_model=KnowledgeBaseResponse)
async def get_knowledge_base(
    kb_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        kb_id_uuid = uuid.UUID(kb_id)

        result = await db.execute(
            select(KnowledgeBase).filter(KnowledgeBase.id == kb_id_uuid)
        )
        kb = result.scalar_one_or_none()
        if not kb:
            raise HTTPException(status_code=404, detail="知识库不存在")

        require_owner(kb.owner_id, current_user)

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
            logger.warning("删除知识库时向量存储清理失败，继续删除数据库记录: %s", exc)

        # 删除 MinIO 中的原始文件（并发执行），失败仅告警不阻塞。
        minio_service = await MinioService.get_instance()

        async def cleanup_minio_file(doc: Document) -> None:
            if not doc.file_path or not doc.file_path.startswith("minio://"):
                return
            try:
                await minio_service.delete_file_async(doc.file_path)
            except Exception as exc:
                logger.warning("删除 MinIO 文件 %s 失败: %s", doc.file_path, exc)

        await asyncio.gather(*(cleanup_minio_file(doc) for doc in docs))

        # 批量删除数据库记录：先删 Document，再删 KnowledgeBase，防止外键冲突。
        await db.execute(delete(Document).filter(Document.kb_id == kb.id))
        await db.execute(delete(KnowledgeBase).filter(KnowledgeBase.id == kb.id))
        await db.commit()

        await invalidate_kb_list_cache(current_user.user_id)

        return {"message": "知识库已删除"}
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的知识库ID")


@router.post("/batch-delete", response_model=BatchDeleteKnowledgeBasesResponse)
async def batch_delete_knowledge_bases(
    request: BatchDeleteKnowledgeBasesRequest,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    批量删除知识库及其关联文档。

    仅删除当前用户拥有的知识库。若待删除列表包含默认知识库，会自动将默认标记
    转移到该用户的其他知识库；若用户选择删除其全部知识库，则返回 400。
    所有数据库操作在同一事务中执行，MinIO 文件与向量清理采用异步并行方式，
    降低长耗时任务对响应时间的影响。
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

    from src.services.minio_service import MinioService

    # 批量删除向量：合并为一个 delete 表达式并统一 flush 一次，
    # 避免每个知识库单独 flush 触发 Milvus 单集合 0.1 QPS 限流。
    # 向量清理失败不应阻塞数据库记录删除，仅记录告警，避免服务抖动导致知识库无法删除。
    if found_ids:
        try:
            vector_store = await VectorStoreManager.get_instance()
            await vector_store.delete_by_kb_ids([str(kb_id) for kb_id in found_ids])
        except Exception as exc:
            logger.warning(
                "批量删除知识库时向量存储清理失败，继续删除数据库记录: %s", exc
            )

    # 删除 MinIO 中的原始文件，按 BATCH_DELETE_CHUNK_SIZE 分批并发，避免单次任务过多。
    # MinIO 清理失败不应阻塞数据库记录删除，仅记录告警。
    doc_result = await db.execute(
        select(Document).filter(Document.kb_id.in_(found_ids))
    )
    docs = doc_result.scalars().all()

    async def cleanup_minio_file(doc: Document) -> None:
        if not doc.file_path or not doc.file_path.startswith("minio://"):
            return
        try:
            minio_service = await MinioService.get_instance()
            await minio_service.delete_file_async(doc.file_path)
        except Exception as exc:
            logger.warning("删除 MinIO 文件 %s 失败: %s", doc.file_path, exc)

    if docs:
        for i in range(0, len(docs), BATCH_DELETE_CHUNK_SIZE):
            batch = docs[i:i + BATCH_DELETE_CHUNK_SIZE]
            await asyncio.gather(*(cleanup_minio_file(doc) for doc in batch))

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


@router.post("/recommend", response_model=List[KBRecommendationResponse])
async def recommend_knowledge_bases(request: KBRecommendationRequest):
    """
    基于问题自动推荐相关知识库

    Args:
        request: 请求数据（问题、返回数量）

    Returns:
        list: 推荐的知识库列表，按相关性排序
    """
    vector_store = await VectorStoreManager.get_instance()
    rag_chain = await RAGChain.get_instance(vector_store)

    try:
        recommendations = await rag_chain.recommend_knowledge_bases(request.question, request.top_k)
        return recommendations
    except Exception as e:
        raise HTTPException(status_code=500, detail="知识库推荐失败，请稍后重试")


class KnowledgeGraphRequest(BaseModel):
    """知识图谱生成请求模型。"""
    kb_ids: Optional[List[str]] = None


@router.post("/knowledge_graph", response_model=KnowledgeGraphResponse)
async def generate_knowledge_graph(request: KnowledgeGraphRequest = Body(...)):
    """
    生成知识库关系图谱

    Args:
        request: 包含知识库ID列表的请求体（可选）

    Returns:
        dict: 知识图谱数据
    """
    vector_store = await VectorStoreManager.get_instance()
    rag_chain = await RAGChain.get_instance(vector_store)

    try:
        result = await rag_chain.generate_knowledge_graph(request.kb_ids)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail="生成知识图谱失败，请稍后重试")