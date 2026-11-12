"""向量存储管理器。

对 MilvusService 进行二次封装，将原始检索结果转换为 LangChain Document 对象，
供 RAG 链统一使用。
"""

import logging

from langchain_core.documents import Document
from pymilvus.exceptions import MilvusException

from src.services.chunk_expand import (
    merge_with_neighbors,
    neighbor_positions,
    same_heading_as_parent,
)
from src.services.milvus_service import MilvusService
from src.utils.async_singleton import AsyncSingleton
from src.utils.error_handling import ErrorSeverity, log_exception

logger = logging.getLogger(__name__)


class VectorStoreManager(AsyncSingleton["VectorStoreManager"]):
    """向量存储管理器，负责 Document 与 Milvus 之间的转换。"""

    def __init__(self):
        self.milvus_service = None

    async def _async_init(self):
        """异步初始化 MilvusService 连接。"""
        self.milvus_service = await MilvusService.get_instance()

    async def create_vector_store(self, documents, kb_id=""):
        """首次创建向量存储并插入文档（语义同 add_documents）。"""
        await self.milvus_service.insert_embeddings(documents, kb_id)
        return self.milvus_service

    async def add_documents(self, documents, kb_id=""):
        """向指定知识库追加文档切片。"""
        await self.milvus_service.insert_embeddings(documents, kb_id)

    async def search(self, query, k=3, document_ids=None, kb_ids=None):
        """检索与查询最相关的文档切片，并包装为 LangChain Document。

        阶段一升级：默认走混合检索（dense + BM25 + RRF + rerank），
        当混合检索未启用或失败时自动回退到纯 dense 检索。

        Args:
            query: 用户查询文本。
            k: 返回结果数量上限。
            document_ids: 可选，限定文档范围。
            kb_ids: 可选，限定知识库范围。

        Returns:
            list[Document]: 包含元数据的 LangChain Document 列表。
        """
        try:
            results = await self.milvus_service.search_hybrid(
                query, k=k, document_ids=document_ids, kb_ids=kb_ids
            )
        except MilvusException as e:
            # 仅捕获 Milvus 已知可恢复异常降级纯向量检索；其它异常（配置错误等）上抛。
            # 降级后少了两路召回，结果排序与命中率都会变——用户只会觉得「检索变差」。
            log_exception(
                logger, e,
                action="vector_store.search_hybrid",
                severity=ErrorSeverity.RECOVERABLE,
                message="混合检索失败，降级为纯向量检索",
                k=k,
                kb_scope=len(kb_ids) if kb_ids else 0,
            )
            results = await self.milvus_service.search(
                query, k=k, document_ids=document_ids, kb_ids=kb_ids
            )
        return self._results_to_documents(results)

    async def search_dense(self, query, k=3, document_ids=None, kb_ids=None, query_embedding=None, source_kind=None, exclude_source_kinds=None):
        """纯 dense 向量检索入口（兼容旧逻辑）。"""
        results = await self.milvus_service.search_dense(
            query, k=k, document_ids=document_ids, kb_ids=kb_ids, query_embedding=query_embedding, source_kind=source_kind, exclude_source_kinds=exclude_source_kinds
        )
        return self._results_to_documents(results)

    async def search_hybrid(self, query, k=3, document_ids=None, kb_ids=None, query_embedding=None, source_kind=None, exclude_source_kinds=None):
        """混合检索入口，返回经 RRF 融合与 Cross-Encoder 重排序后的 Document。"""
        results = await self.milvus_service.search_hybrid(
            query, k=k, document_ids=document_ids, kb_ids=kb_ids, query_embedding=query_embedding, source_kind=source_kind, exclude_source_kinds=exclude_source_kinds
        )
        return self._results_to_documents(results)

    async def search_hybrid_multi(self, queries, k=3, document_ids=None, kb_ids=None, query_embedding=None, source_kind=None, exclude_source_kinds=None):
        """多查询并行混合检索入口（P2-2），返回经跨查询 RRF 融合与重排序后的 Document。

        queries[0] 必须为原始（或消解后）问题，作为 rerank 语义基准。
        """
        results = await self.milvus_service.search_hybrid_multi(
            queries, k=k, document_ids=document_ids, kb_ids=kb_ids, query_embedding=query_embedding, source_kind=source_kind, exclude_source_kinds=exclude_source_kinds
        )
        return self._results_to_documents(results)

    async def expand_neighbors(self, docs, budget_chars=None):
        """给选中的块补上同文档的 `chunk_index ± 1`（邻块回填）。

        调用点必须在**截断到 TOP_K 之后**：补进来的邻块排在原地块后面，
        先补后截断等于把它们全部截掉（离线脚本是在探针结果上直接算的，
        接进生产时这是最容易出错的一处）。

        Args:
            docs: 已按相关性排序、已截断的 Document 列表。
            budget_chars: 总字符上限（含原地块），None 表示不限。见 chunk_expand 的实测数据。

        Returns:
            原地块 + 邻块。补查失败时原样返回原地块（回填是增益，不是依赖）。
        """
        if not docs or budget_chars == 0:
            return docs
        # 用 (document_id, chunk_index) 当 id：切片主键是 uuid、每次重灌都会变，
        # 位置才是稳定身份；而且相邻的两个原地块互为邻块时，靠它去重才不会补出重复块。
        base = []
        for d in docs:
            doc_id = str(d.metadata.get("document_id", ""))
            idx = d.metadata.get("chunk_index")
            if not doc_id or idx is None:
                continue
            base.append({
                "id": f"{doc_id}#{int(idx)}",
                "document_id": doc_id,
                "chunk_index": int(idx),
                "content": d.page_content,
                "heading_path": str(d.metadata.get("heading_path") or ""),
                "_doc": d,
            })
        if not base:
            return docs
        positions = neighbor_positions(base)
        if not positions:
            return docs
        try:
            rows = await self.milvus_service.fetch_chunks_by_positions(positions)
        except Exception as e:  # noqa: BLE001 —— 回填失败不能影响检索主链路
            log_exception(
                logger, e, action="vector_store.expand_neighbors",
                severity=ErrorSeverity.RECOVERABLE,
                message="邻块回填失败，返回原检索结果",
                positions=len(positions),
            )
            return docs
        if not rows:
            return docs

        for row in rows:
            row["id"] = f'{row.get("document_id")}#{int(row.get("chunk_index", -1))}'
        # 物理相邻 ≠ 语义连续：只补同章节的邻块，跨章节的相邻块会把注意力带偏
        # （实测代价见 chunk_expand.same_heading_as_parent 的说明）
        rows = same_heading_as_parent(rows, base)
        if not rows:
            return docs
        merged = merge_with_neighbors(base, rows, budget_chars=budget_chars)

        # 邻块没有自己的重排分：沿用「带它进来的那个原地块」的分数。
        # 不编一个 0 分，否则下游按分数排序或做相关性门禁时，邻块会莫名其妙沉底/被清掉。
        parent_score = {
            (str(d.metadata.get("document_id", "")), int(d.metadata.get("chunk_index") or -1)): float(
                d.metadata.get("rerank_score") or 0.0
            )
            for d in docs
        }
        out = []
        n_added = 0
        for item in merged:
            if item.get("_doc") is not None:
                out.append(item["_doc"])
                continue
            doc = self._results_to_documents([item])[0]
            key = (item.get("document_id", ""), int(item.get("chunk_index") or -1))
            score = parent_score.get((key[0], key[1] - 1), parent_score.get((key[0], key[1] + 1), 0.0))
            doc.metadata["rerank_score"] = score
            doc.metadata["score"] = score
            doc.metadata["neighbor_expand"] = True
            out.append(doc)
            n_added += 1
        logger.debug(
            "邻块回填：%d 块 → %d 块（+%d，预算 %s 字符）",
            len(docs), len(out), n_added, budget_chars,
        )
        return out

    def _results_to_documents(self, results):
        """将 Milvus 检索结果统一转换为 LangChain Document。"""
        docs = []
        for result in results:
            doc = Document(
                page_content=result["content"],
                metadata={
                    "kb_id": result.get("kb_id", ""),
                    "document_id": result.get("document_id", ""),
                    "source": result.get("source", ""),
                    "heading_path": result.get("heading_path", ""),
                    "chunk_index": result.get("chunk_index", 0),
                    "source_kind": result.get("source_kind", "raw"),
                    "score": result.get("rerank_score", result.get("rrf_score", result.get("score", 0.0))),
                    "dense_score": result.get("dense_score", 0.0),
                    "sparse_score": result.get("sparse_score", 0.0),
                    "rrf_score": result.get("rrf_score", 0.0),
                    "rerank_score": result.get("rerank_score", 0.0),
                    # 重排整批失败时 hybrid_search 回退 RRF，并把 flag 透传到这里：
                    # 下游据此知道「rerank_score 不是相关性分」，不能拿它做相关性门禁
                    "rerank_fallback": bool(result.get("rerank_fallback", False)),
                }
            )
            docs.append(doc)
        return docs

    async def get_chunks_by_document_id(self, document_id):
        """获取指定文档（含 Wiki 页）的全部分块，包装为 Document（Wiki 链接扩展用）。"""
        results = await self.milvus_service.get_document_chunks(document_id)
        return self._results_to_documents(results)

    async def delete_by_document_id(self, document_id):
        """按文档 ID 删除向量数据。"""
        await self.milvus_service.delete_by_document_id(document_id)

    async def delete_by_document_ids(self, document_ids):
        """按多个文档 ID 批量删除向量数据，并统一 flush 一次。"""
        await self.milvus_service.delete_by_document_ids(document_ids)

    async def delete_by_kb_id(self, kb_id):
        """按知识库 ID 删除向量数据。"""
        await self.milvus_service.delete_by_kb_id(kb_id)

    async def delete_by_kb_ids(self, kb_ids):
        """按多个知识库 ID 批量删除向量数据，并统一 flush 一次。"""
        await self.milvus_service.delete_by_kb_ids(kb_ids)

    async def flush(self):
        """显式触发 Milvus flush。"""
        await self.milvus_service.flush()

    async def count(self):
        """返回全部切片数量。"""
        return await self.milvus_service.count()

    async def count_by_kb(self, kb_id):
        """返回指定知识库的切片数量。"""
        return await self.milvus_service.count_by_kb(kb_id)
