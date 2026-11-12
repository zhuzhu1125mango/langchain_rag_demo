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
from src.utils.error_handling import ErrorSeverity, log_exception

logger = logging.getLogger("rag_system")

try:
    from src.middleware.prometheus import record_vector_search
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False


def _configured_exclude_kinds() -> tuple:
    """解析配置的主检索排除 source_kind 集合（逗号分隔；空配置 → 不排除）。

    E-1 W2：默认由 KB_RETRIEVAL_EXCLUDE_SOURCE_KINDS 驱动——
    现状 "wiki_syn"（entity/topic 编译页仍在主池）vs 退池 "wiki,wiki_syn"。
    """
    raw = settings.processing.KB_RETRIEVAL_EXCLUDE_SOURCE_KINDS or ""
    return tuple(k.strip() for k in raw.split(",") if k.strip())


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
                # 检索返回空会让上游把「服务挂了」误判成「知识库没内容」
                log_exception(
                    logger, e,
                    action="kb.retrieval.vector_store",
                    message="向量存储初始化失败，本次检索返回空结果",
                )
        return self._vector_store

    async def retrieve(
        self,
        question: str,
        kb_ids: Optional[List[str]] = None,
        document_ids: Optional[List[str]] = None,
        query_embedding: Optional[list] = None,
        queries: Optional[List[str]] = None,
        source_kind: Optional[str] = None,
        exclude_source_kinds: Optional[tuple] = None,
    ):
        """根据问题检索相关文档。

        Args:
            question: 用户问题
            kb_ids: 指定的知识库ID列表（可选）
            document_ids: 指定的文档ID列表（可选）
            query_embedding: 预计算的 query 向量（语义缓存场景），传入时跳过重复计算
            queries: 改写后的多查询列表（可选，首元素必须为 question）
            source_kind: 可选，限定切片来源类型（如 "wiki" 只检索编译页）
            exclude_source_kinds: 可选，排除的切片来源类型集合。缺省（None）时取配置
                `KB_RETRIEVAL_EXCLUDE_SOURCE_KINDS`——默认 "wiki,wiki_syn"（退池）：
                编译页（entity/topic + synthesis）不与原始 chunk 同池竞争，仅经
                wiki_lookup 导航入口触达（E-1 W2；现状口径为仅排 "wiki_syn"）。

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
            elif exclude_source_kinds is not None:
                search_kwargs["exclude_source_kinds"] = tuple(exclude_source_kinds)
            else:
                # 默认排除集合由配置驱动（E-1 W2：现状仅排 wiki_syn；退池再加 wiki）
                search_kwargs["exclude_source_kinds"] = _configured_exclude_kinds()

            if settings.processing.KB_ENABLE_HYBRID_SEARCH:
                try:
                    if queries and len(queries) > 1 and settings.processing.KB_MULTI_QUERY_ENABLED:
                        docs = await vector_store.search_hybrid_multi(queries, **search_kwargs)
                    else:
                        docs = await vector_store.search_hybrid(question, **search_kwargs)
                except Exception as e:
                    log_exception(
                        logger, e,
                        action="kb.retrieval.hybrid",
                        severity=ErrorSeverity.RECOVERABLE,
                        message="混合检索失败，回退到 dense 检索",
                        top_k=search_kwargs.get("k"),
                    )
                    try:
                        docs = await vector_store.search_dense(question, **search_kwargs)
                    except Exception as e2:
                        # 两级检索都失败：本次问答必然无命中，必须留痕后上抛。
                        # severity 显式写 REPORTABLE，不能交给 classify_exception 按异常类型判：
                        # pymilvus 的异常在分类器里属于「可恢复」（本意是单次抖动可重试），
                        # 但这里两级都挂了、本次问答直接不可用，与异常类型无关。
                        # 2026-11-07 故障演练（pause Milvus）实测：不显式指定时这条只打
                        # WARNING + severity=recoverable 且不带堆栈，与单测结论相反。
                        log_exception(
                            logger, e2,
                            action="kb.retrieval.dense_fallback",
                            severity=ErrorSeverity.REPORTABLE,
                            message="dense 兜底检索同样失败，本次检索无结果",
                        )
                        # P2-5：检索失败埋点（best-effort，埋点自身失败不阻断上抛）
                        from src.middleware.prometheus import record_retrieval_error
                        try:
                            record_retrieval_error()
                        except Exception:
                            pass  # 指标计数失败不影响错误上抛语义
                        raise e2 from e
            else:
                docs = await vector_store.search_dense(question, **search_kwargs)

            docs = docs[: settings.processing.TOP_K]
            # 邻块回填必须在截断之后：补进来的块排在原地块后面，先补后截断等于白补。
            # 开关关闭时是完全无副作用的旁路（不查库、不改顺序）。
            if settings.processing.KB_NEIGHBOR_EXPAND_ENABLED and docs:
                t_expand = time.time()
                before = len(docs)
                docs = await vector_store.expand_neighbors(
                    docs,
                    budget_chars=settings.processing.KB_NEIGHBOR_EXPAND_BUDGET_CHARS,
                )
                # 回填的耗时与增量要能看见：否则「检索变慢」或「上下文变长」都会
                # 被归因到别的地方（历史上 rerank/embed 的锅都是这么背上的）。
                try:
                    from src.services.pipeline.state import _request_retrieval_timings

                    timings = dict(_request_retrieval_timings.get() or {})
                    timings["neighbor_expand_ms"] = int((time.time() - t_expand) * 1000)
                    timings["neighbor_expand_added"] = len(docs) - before
                    _request_retrieval_timings.set(timings)
                except Exception:  # noqa: BLE001 —— 埋点失败不影响检索
                    pass
            return docs
        finally:
            if PROMETHEUS_AVAILABLE:
                record_vector_search(time.time() - search_start)
