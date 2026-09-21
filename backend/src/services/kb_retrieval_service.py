"""知识库检索服务。

将原 RAGChain._retrieve_documents 的检索逻辑下沉为独立服务，
供问答管线（rag_chain）与 Agent 工具（kb_search 等）共用同一检索口径，
避免"管线一套、工具一套"的检索行为漂移。

行为与原实现逐行对齐：
- 优先混合检索（dense + BM25 + RRF + rerank），失败自动回退纯 dense
- 多查询开启且 queries>1 时走多查询并行检索（跨查询 RRF 融合，rerank 用原始问题）
- 结果截断到 settings.processing.TOP_K
"""

import time
import logging
from typing import List, Optional

from src.config import settings

logger = logging.getLogger("rag_system")

try:
    from src.middleware.prometheus import record_vector_search
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False


class KBRetrievalService:
    """统一知识库检索入口（管线与 Agent 工具共用）。"""

    def __init__(self, vector_store=None):
        """
        Args:
            vector_store: VectorStoreManager 实例（可选，注入便于测试）；
                          缺省时懒加载全局单例。
        """
        self._vector_store = vector_store

    async def _ensure_vector_store(self):
        """获取向量存储实例；注入实例为空时懒加载单例。"""
        if self._vector_store is None:
            from .vector_store import VectorStoreManager
            try:
                self._vector_store = await VectorStoreManager.get_instance()
            except Exception as e:
                logger.warning(f"向量存储初始化失败，检索返回空: {e}")
        return self._vector_store

    async def retrieve(
        self,
        question: str,
        kb_ids: Optional[List[str]] = None,
        document_ids: Optional[List[str]] = None,
        query_embedding: Optional[list] = None,
        queries: Optional[List[str]] = None,
        source_kind: Optional[str] = None,
    ):
        """根据问题检索相关文档。

        Args:
            question: 用户问题
            kb_ids: 指定的知识库ID列表（可选）
            document_ids: 指定的文档ID列表（可选）
            query_embedding: 预计算的 query 向量（语义缓存场景），传入时跳过重复计算
            queries: 改写后的多查询列表（可选，首元素必须为 question）
            source_kind: 可选，限定切片来源类型（如 "wiki" 只检索编译页）

        Returns:
            list: 检索到的 Document 对象列表（截断到 TOP_K）
        """
        vector_store = await self._ensure_vector_store()
        if vector_store is None:
            return []

        search_start = time.time()
        try:
            search_kwargs = {"k": settings.processing.TOP_K}
            if kb_ids and len(kb_ids) > 0:
                search_kwargs["kb_ids"] = kb_ids
            elif document_ids and len(document_ids) > 0:
                search_kwargs["document_ids"] = document_ids
            if query_embedding is not None:
                search_kwargs["query_embedding"] = query_embedding
            if source_kind:
                search_kwargs["source_kind"] = source_kind

            if settings.processing.KB_ENABLE_HYBRID_SEARCH:
                try:
                    if queries and len(queries) > 1 and settings.processing.KB_MULTI_QUERY_ENABLED:
                        docs = await vector_store.search_hybrid_multi(queries, **search_kwargs)
                    else:
                        docs = await vector_store.search_hybrid(question, **search_kwargs)
                except Exception as e:
                    logger.warning(f"混合检索失败，回退到 dense 检索: {e}")
                    docs = await vector_store.search_dense(question, **search_kwargs)
            else:
                docs = await vector_store.search_dense(question, **search_kwargs)

            return docs[: settings.processing.TOP_K]
        finally:
            if PROMETHEUS_AVAILABLE:
                record_vector_search(time.time() - search_start)
