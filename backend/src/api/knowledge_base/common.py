"""知识库 API 共享层：请求/响应模型、默认库切换、列表缓存失效与归属过滤（CODE-1 拆分）。"""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from pydantic import BaseModel
from typing import List, Literal, Optional
from datetime import timedelta
import uuid
import logging
from src.auth import CurrentUser
from src.models import KnowledgeBase
from src.services.cache_service import CacheService

logger = logging.getLogger("knowledge_base_api")

KB_LIST_CACHE_PREFIX = "kb:list"
KB_LIST_CACHE_TTL = timedelta(seconds=60)
BATCH_DELETE_MAX_IDS = 1000
BATCH_DELETE_CHUNK_SIZE = 800

# 缓存操作兜底超时（秒）。即使 Redis 客户端自身已配置超时，
# 这里再加一层保护，确保任何情况下知识库列表接口都不会被缓存阻塞。
KB_CACHE_OPERATION_TIMEOUT = 2.0

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
    # W6 #28：Literal 限制合法值（非法值 422），与 KB.status 的 active/inactive 对齐
    status: Optional[Literal["active", "inactive"]] = None
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
class KnowledgeBaseListResponse(BaseModel):
    """知识库列表响应模型。"""
    items: List[KnowledgeBaseResponse]
    total: int
    page: int
    page_size: int
    total_pages: int
class KnowledgeGraphRequest(BaseModel):
    """知识图谱生成请求模型。"""
    kb_ids: Optional[List[str]] = None

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

async def _owned_kb_ids(db: AsyncSession, current_user: CurrentUser) -> List[str]:
    """取当前用户拥有的全部知识库 ID（可能为空列表）。

    调用方在结果为空时**必须短路返回**，不可把空列表继续下传：
    检索层把空 kb_ids 当作"不限定范围"，会退化为跨用户的全量检索。
    """
    result = await db.execute(
        select(KnowledgeBase.id).filter(KnowledgeBase.owner_id == current_user.user_id)
    )
    return [str(row) for row in result.scalars()]
