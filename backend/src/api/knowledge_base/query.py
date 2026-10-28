"""知识库查询路由：列表/默认库/详情/推荐/知识图谱（CODE-1 拆分）。"""

from fastapi import APIRouter, Depends, HTTPException, Body, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from typing import List
from datetime import timedelta
import uuid
import asyncio
import logging
from src.database import get_db
from src.auth import get_current_user, CurrentUser, require_owner
from src.utils.validators import parse_uuid_list, validate_kb_ownership
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

from src.services.cache_service import CacheService
from src.services.vector_store import VectorStoreManager
from src.services.rag_chain import RAGChain

from .common import (
    KBRecommendationRequest,
    KBRecommendationResponse,
    KnowledgeBaseListResponse,
    KnowledgeGraphRequest,
    KnowledgeBaseResponse,
    KnowledgeGraphResponse,
    _owned_kb_ids,
    _kb_list_cache_key,
)

router = APIRouter(tags=["knowledge_bases"])

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

@router.post("/recommend", response_model=List[KBRecommendationResponse])
async def recommend_knowledge_bases(
    request: KBRecommendationRequest,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    基于问题自动推荐相关知识库（候选范围限定为当前用户拥有的知识库）

    Args:
        request: 请求数据（问题、返回数量）
        current_user: 当前认证用户
        db: 数据库会话

    Returns:
        list: 推荐的知识库列表，按相关性排序
    """
    # 候选知识库由服务端按 owner 解析，不接受客户端指定
    owned_kb_ids = await _owned_kb_ids(db, current_user)
    if not owned_kb_ids:
        return []

    vector_store = await VectorStoreManager.get_instance()
    rag_chain = await RAGChain.get_instance(vector_store)

    try:
        recommendations = await rag_chain.recommend_knowledge_bases(
            request.question, request.top_k, kb_ids=owned_kb_ids
        )
        return recommendations
    except Exception as e:
        logger.error(f"知识库推荐失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="知识库推荐失败，请稍后重试")

@router.post("/knowledge_graph", response_model=KnowledgeGraphResponse)
async def generate_knowledge_graph(
    request: KnowledgeGraphRequest = Body(...),
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    生成知识库关系图谱（范围限定为当前用户拥有的知识库）

    Args:
        request: 包含知识库ID列表的请求体（可选；不传则取当前用户全部知识库）
        current_user: 当前认证用户
        db: 数据库会话

    Returns:
        dict: 知识图谱数据
    """
    if request.kb_ids:
        # 显式指定时校验归属：含非本人知识库则整体拒绝（403）
        kb_ids = parse_uuid_list(request.kb_ids, name="kb_id")
        await validate_kb_ownership(db, kb_ids, current_user)
    else:
        kb_ids = await _owned_kb_ids(db, current_user)

    # 空列表必须短路：检索层对空 kb_ids 不加过滤条件，等价于全量检索
    if not kb_ids:
        return {"nodes": [], "edges": [], "summary": "没有可用的知识库"}

    vector_store = await VectorStoreManager.get_instance()
    rag_chain = await RAGChain.get_instance(vector_store)

    try:
        result = await rag_chain.generate_knowledge_graph(kb_ids)
        return result
    except Exception as e:
        logger.error(f"生成知识图谱失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="生成知识图谱失败，请稍后重试")
