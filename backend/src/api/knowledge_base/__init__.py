"""知识库管理 API（CODE-1 拆分后为包）。

子模块职责：
- ``common`` 请求/响应模型、默认库切换、列表缓存失效、归属过滤（无路由）
- ``query``  列表、默认库、详情、推荐、知识图谱
- ``manage`` 创建、更新、删除、批量删除、设为默认

对外 ``router`` 前缀与标签不变；模型与 ``invalidate_kb_list_cache`` /
``set_single_default`` 保留包级导出（document 侧与单测直接引用）。
"""

from fastapi import APIRouter

from . import manage, query

router = APIRouter(prefix="/knowledge_bases", tags=["knowledge_bases"])

# 静态路径（/default）先于 /{kb_id} 注册，避免被动态路由吞掉
router.include_router(query.router)
router.include_router(manage.router)

# 兼容历史导入路径
from .common import (  # noqa: E402
    BatchDeleteKnowledgeBasesRequest,
    BatchDeleteKnowledgeBasesResponse,
    KBRecommendationRequest,
    KBRecommendationResponse,
    KnowledgeBaseCreate,
    KnowledgeBaseListResponse,
    KnowledgeBaseResponse,
    KnowledgeBaseUpdate,
    KnowledgeGraphRequest,
    KnowledgeGraphResponse,
    _owned_kb_ids,
    invalidate_kb_list_cache,
    set_single_default,
)
from .manage import (  # noqa: E402
    batch_delete_knowledge_bases,
    create_knowledge_base,
    delete_knowledge_base,
    set_default_knowledge_base,
    update_knowledge_base,
)
from .query import (  # noqa: E402
    generate_knowledge_graph,
    get_default_knowledge_base,
    get_knowledge_base,
    list_knowledge_bases,
    recommend_knowledge_bases,
)

__all__ = [
    "router",
    "KnowledgeBaseCreate",
    "KnowledgeBaseUpdate",
    "KnowledgeBaseResponse",
    "KnowledgeBaseListResponse",
    "KBRecommendationRequest",
    "KBRecommendationResponse",
    "KnowledgeGraphRequest",
    "KnowledgeGraphResponse",
    "BatchDeleteKnowledgeBasesRequest",
    "BatchDeleteKnowledgeBasesResponse",
    "set_single_default",
    "invalidate_kb_list_cache",
    "_owned_kb_ids",
    "create_knowledge_base",
    "update_knowledge_base",
    "delete_knowledge_base",
    "batch_delete_knowledge_bases",
    "set_default_knowledge_base",
    "list_knowledge_bases",
    "get_default_knowledge_base",
    "get_knowledge_base",
    "recommend_knowledge_bases",
    "generate_knowledge_graph",
]
