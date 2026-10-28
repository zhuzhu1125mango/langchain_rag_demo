"""Milvus 检索：dense / sparse / 混合与结果规整（CODE-1 拆分）。"""

import asyncio
import logging
import re
import time
from typing import Any, Dict


from src.config import settings
from src.services.milvus_common import (
    _ab_rerank_top_k,
)

logger = logging.getLogger("milvus_service")


class SearchMixin:
    """向量检索与过滤表达式构造，由 MilvusService 组合。"""

    async def search(self, query, k=3, document_ids=None, kb_ids=None):
        """基于向量相似度检索相关切片（兼容旧接口，等价于 search_dense）。

        Args:
            query: 用户查询文本。
            k: 返回结果数量上限。
            document_ids: 可选，限定只在这些文档中检索。
            kb_ids: 可选，限定只在这些知识库中检索。

        Returns:
            list[dict]: 包含 content/document_id/kb_id/source/score 等字段的结果列表。
        """
        return await self.search_dense(query, k=k, document_ids=document_ids, kb_ids=kb_ids)

    async def search_dense(self, query, k=3, document_ids=None, kb_ids=None, query_embedding=None, source_kind=None, exclude_source_kinds=None):
        """Dense 向量检索通道。

        Args:
            query_embedding: 预计算的 query 向量，传入时跳过 aembed_query 避免重复计算。
        """
        if query_embedding is None:
            query_embedding = await self._get_embeddings().aembed_query(query)

        search_params = {
            "metric_type": "IP",
            "params": {"ef": settings.milvus.MILVUS_EF}
        }
        filter_expr = self._build_filter_expr(document_ids, kb_ids, source_kind=source_kind, exclude_source_kinds=exclude_source_kinds)

        results = await self.client.search(
            collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
            data=[query_embedding],
            anns_field="embedding",
            search_params=search_params,
            limit=k,
            filter=filter_expr,
            output_fields=self._base_output_fields()
        )
        return self._parse_search_results(results)

    async def search_sparse(self, query, k=3, document_ids=None, kb_ids=None, source_kind=None, exclude_source_kinds=None):
        """Sparse BM25 关键词检索通道。

        若 BM25 未启用或初始化失败，返回空列表并记录日志。
        """
        if not self._sparse_enabled or not self.bm25_ef:
            logger.debug("BM25 sparse 检索未启用或未初始化，返回空结果")
            return []

        try:
            query_sparse = await asyncio.to_thread(self.bm25_ef.encode_queries, [query])
            query_sparse = self._convert_sparse_embeddings(query_sparse)
            if not query_sparse:
                return []
        except Exception as e:
            logger.warning(f"BM25 查询编码失败: {e}")
            return []

        search_params = {
            "metric_type": "IP",
            "params": {"drop_ratio_search": 0.2}
        }
        filter_expr = self._build_filter_expr(document_ids, kb_ids, source_kind=source_kind, exclude_source_kinds=exclude_source_kinds)

        try:
            results = await self.client.search(
                collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
                data=query_sparse,
                anns_field="sparse_embedding",
                search_params=search_params,
                limit=k,
                filter=filter_expr,
                output_fields=self._base_output_fields()
            )
            return self._parse_search_results(results)
        except Exception as e:
            logger.warning(f"Sparse 检索失败: {e}")
            return []

    async def search_hybrid(self, query, k=3, document_ids=None, kb_ids=None, query_embedding=None, source_kind=None, exclude_source_kinds=None):
        """混合检索入口：dense + sparse，返回经 RRF 融合与 Cross-Encoder 重排序后的结果。

        Args:
            query: 用户查询文本。
            k: 最终返回结果数量上限。
            document_ids: 可选，限定只在这些文档中检索。
            kb_ids: 可选，限定只在这些知识库中检索。
            query_embedding: 预计算的 query 向量，传入时跳过 aembed_query 避免重复计算。
            source_kind: 可选，限定切片来源类型（如 "wiki" 只检索编译页）。
            exclude_source_kinds: 可选，排除的切片来源类型集合（如 synthesis 页不混入主检索）。

        Returns:
            list[dict]: 包含额外字段 `dense_score` / `sparse_score` / `rrf_score` / `rerank_score` 的结果列表。
        """
        from src.services.hybrid_search import reciprocal_rank_fusion, rerank_results

        # kb_retrieval 细分计时：写入请求级 ContextVar，由管线 kb_retrieval 阶段消费
        # 写入 trace stage detail（函数内 import 防循环依赖，同 hybrid_search 惯例）
        from src.services.pipeline.state import _request_retrieval_timings

        top_k = max(k, settings.processing.KB_HYBRID_SEARCH_TOP_K)
        timings: Dict[str, Any] = {}

        async def _timed(coro, key: str):
            t0 = time.perf_counter()
            try:
                return await coro
            finally:
                timings[key] = int((time.perf_counter() - t0) * 1000)

        try:
            # embed 单独计时：未预计算时在此先算再传给 search_dense（语义等价），
            # 已预计算（语义缓存复用）时 embed_ms=0
            if query_embedding is None:
                t0 = time.perf_counter()
                query_embedding = await self.embeddings.aembed_query(query)
                timings["embed_ms"] = int((time.perf_counter() - t0) * 1000)
            else:
                timings["embed_ms"] = 0

            # B2：dense/sparse 检索彼此独立，并行执行减少 IO 串行等待
            dense_results, sparse_results = await asyncio.gather(
                _timed(
                    self.search_dense(query, k=top_k, document_ids=document_ids, kb_ids=kb_ids, query_embedding=query_embedding, source_kind=source_kind, exclude_source_kinds=exclude_source_kinds),
                    "dense_ms",
                ),
                _timed(
                    self.search_sparse(query, k=top_k, document_ids=document_ids, kb_ids=kb_ids, source_kind=source_kind, exclude_source_kinds=exclude_source_kinds),
                    "sparse_ms",
                ),
            )

            t0 = time.perf_counter()
            fused = reciprocal_rank_fusion(
                {"dense": dense_results, "sparse": sparse_results},
                k=settings.processing.KB_RRF_K,
            )
            timings["rrf_ms"] = int((time.perf_counter() - t0) * 1000)

            # C1：rerank 关闭或未配置模型时，直接按 RRF 分数截断，不加载/调用 reranker
            if (
                not settings.processing.KB_RERANK_ENABLED
                or not settings.processing.KB_RERANK_MODEL
            ):
                timings["rerank_skipped"] = True
                ranked = sorted(fused, key=lambda x: x.get("rrf_score", 0.0), reverse=True)
                return [dict(r, rerank_score=r.get("rrf_score", 0.0)) for r in ranked[:k]]

            # C1：RRF 融合后先截断到 rerank 候选数再送打分，避免对全部融合候选逐条调用重排序
            rerank_n = max(k, _ab_rerank_top_k())
            candidates = fused[:rerank_n]
            timings["rerank_pairs"] = len(candidates)
            reranked = await _timed(rerank_results(query, candidates, top_k=k), "rerank_ms")
            return reranked
        finally:
            # 异常路径也落已累计部分，便于归因检索慢在哪一步
            _request_retrieval_timings.set(timings)

    async def search_hybrid_multi(self, queries, k=3, document_ids=None, kb_ids=None, query_embedding=None, source_kind=None, exclude_source_kinds=None):
        """多查询并行混合检索（P2-2）：N query × (dense+sparse) → 跨查询 RRF → 单次 rerank。

        与 search_hybrid 的区别：接收改写后的多查询列表并行召回，
        同一 chunk 命中多个查询通道时 rrf_score 累加（多查询共识加分）；
        rerank 语义基准固定用 queries[0]（原始/消解后问题），rerank 调用次数不变。

        Args:
            queries: 查询列表，第一个元素必须为原始（或消解后）问题。
            k: 最终返回结果数量上限。
            document_ids: 可选，限定只在这些文档中检索。
            kb_ids: 可选，限定只在这些知识库中检索。
            query_embedding: queries[0] 的预计算向量（来自语义缓存查找），传入时跳过重复计算。

        Returns:
            list[dict]: 字段同 search_hybrid。
        """
        from src.services.hybrid_search import reciprocal_rank_fusion, rerank_results

        queries = [q for q in queries if q and q.strip()]
        if not queries:
            return []
        top_k = max(k, settings.processing.KB_MULTI_QUERY_CHANNEL_TOP_K)

        # N × (dense + sparse) 并行召回；dense 通道失败不阻塞其他查询
        tasks = []
        for i, q in enumerate(queries):
            emb = query_embedding if i == 0 else None
            tasks.append(self.search_dense(q, k=top_k, document_ids=document_ids, kb_ids=kb_ids, query_embedding=emb, source_kind=source_kind, exclude_source_kinds=exclude_source_kinds))
            tasks.append(self.search_sparse(q, k=top_k, document_ids=document_ids, kb_ids=kb_ids, source_kind=source_kind, exclude_source_kinds=exclude_source_kinds))
        channel_outputs = await asyncio.gather(*tasks, return_exceptions=True)

        # 单通道失败降级：可用通道照常融合（sparse 通道内部已自行兜底为空列表）
        channel_results = {}
        for i in range(len(queries)):
            dense_res, sparse_res = channel_outputs[i * 2], channel_outputs[i * 2 + 1]
            if isinstance(dense_res, Exception):
                logger.warning(f"多查询 dense 通道失败（query {i}），降级跳过: {dense_res}")
                dense_res = []
            channel_results[f"q{i}_dense"] = dense_res
            channel_results[f"q{i}_sparse"] = sparse_res

        fused = reciprocal_rank_fusion(channel_results, k=settings.processing.KB_RRF_K)

        # rerank 关闭或未配置模型时，直接按 RRF 分数截断（与单查询路径行为一致）
        if (
            not settings.processing.KB_RERANK_ENABLED
            or not settings.processing.KB_RERANK_MODEL
        ):
            ranked = sorted(fused, key=lambda x: x.get("rrf_score", 0.0), reverse=True)
            return [dict(r, rerank_score=r.get("rrf_score", 0.0)) for r in ranked[:k]]

        rerank_n = max(k, _ab_rerank_top_k())
        reranked = await rerank_results(queries[0], fused[:rerank_n], top_k=k)
        return reranked

    # 过滤表达式仅接受 UUID 格式的 ID（防御表达式注入的兜底校验）
    _UUID_RE = re.compile(
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    )

    @classmethod
    def _safe_id(cls, value: Any) -> str:
        """校验 ID 为 UUID 后返回其字符串形式，非法输入直接拒绝。

        Milvus filter 表达式无参数化机制，ID 一律先经此白名单校验再拼接，
        防止构造恶意字符串篡改过滤语义。

        Raises:
            ValueError: ID 不符合 UUID 格式。
        """
        v = str(value)
        if not cls._UUID_RE.match(v):
            raise ValueError(f"非法 ID，已拒绝进入过滤表达式: {v[:64]}")
        return v

    @classmethod
    def _build_filter_expr(cls, document_ids=None, kb_ids=None, source_kind=None, exclude_source_kinds=None):
        """根据 document_ids、kb_ids 与 source_kind 构建 Milvus filter 表达式。

        source_kind / exclude_source_kinds 仅允许字母/数字/下划线（≤16 字符，
        与 schema 一致），防止拼接注入篡改过滤语义。
        """
        conditions = []
        if kb_ids and len(kb_ids) > 0:
            conditions.append(f"kb_id in [{','.join([f'\"{cls._safe_id(kb)}\"' for kb in kb_ids])}]")
        if document_ids and len(document_ids) > 0:
            conditions.append(f"document_id in [{','.join([f'\"{cls._safe_id(doc_id)}\"' for doc_id in document_ids])}]")
        if source_kind:
            sk = str(source_kind)
            if len(sk) > 16 or not all(c.isalnum() or c == "_" for c in sk):
                raise ValueError(f"非法 source_kind，已拒绝进入过滤表达式: {sk[:32]}")
            conditions.append(f'source_kind == "{sk}"')
        if exclude_source_kinds:
            kinds = []
            for sk in exclude_source_kinds:
                sk = str(sk)
                if len(sk) > 16 or not all(c.isalnum() or c == "_" for c in sk):
                    raise ValueError(f"非法 exclude_source_kind，已拒绝进入过滤表达式: {sk[:32]}")
                kinds.append(f'"{sk}"')
            conditions.append(f"source_kind not in [{','.join(kinds)}]")
        return " && ".join(conditions) if conditions else None

    @staticmethod
    def _parse_search_results(results):
        """统一解析 Milvus search 返回结果。"""
        docs = []
        if not results or not results[0]:
            return docs
        for hit in results[0]:
            doc = {
                "id": hit["id"],
                "kb_id": hit["entity"].get("kb_id"),
                "document_id": hit["entity"].get("document_id"),
                "content": hit["entity"].get("content"),
                "source": hit["entity"].get("source"),
                "heading_path": hit["entity"].get("heading_path", ""),
                "chunk_index": hit["entity"].get("chunk_index"),
                "source_kind": hit["entity"].get("source_kind", "raw"),
                "score": hit["distance"]
            }
            docs.append(doc)
        return docs
