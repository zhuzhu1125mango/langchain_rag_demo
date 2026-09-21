"""P2-2 多查询并行检索 + RRF 融合单元测试。

覆盖：
- reciprocal_rank_fusion 对带序号通道名（q{i}_dense / q{i}_sparse）的兼容
- MilvusService.search_hybrid_multi：并行召回、跨查询融合去重、
  rerank 用原始问题、rerank 关闭截断、单通道失败降级
- RAGChain._retrieve_documents 的多查询/单查询路由与开关
"""

import pytest

from src.config import settings
from src.services.hybrid_search import reciprocal_rank_fusion
from src.services.milvus_service import MilvusService
from src.services.rag_chain import RAGChain
from src.services.vector_store import VectorStoreManager


def _make_result(doc_id, chunk_index, score):
    return {
        "document_id": doc_id,
        "chunk_index": chunk_index,
        "content": f"{doc_id}-{chunk_index}",
        "score": score,
    }


class TestReciprocalRankFusionChannels:
    """RRF 通道名兼容与跨查询融合。"""

    def test_legacy_dense_sparse_channels(self):
        """旧通道名 dense/sparse 的分数记录不受影响（回归保护）。"""
        fused = reciprocal_rank_fusion(
            {
                "dense": [_make_result("d1", 0, 0.9)],
                "sparse": [_make_result("d2", 0, 0.8)],
            }
        )
        by_doc = {r["document_id"]: r for r in fused}
        assert by_doc["d1"]["dense_score"] == 0.9
        assert by_doc["d1"]["sparse_score"] == 0.0
        assert by_doc["d2"]["sparse_score"] == 0.8

    def test_indexed_channel_names(self):
        """带查询序号的通道名（q1_dense / q2_sparse）按后缀识别。"""
        fused = reciprocal_rank_fusion(
            {
                "q0_dense": [_make_result("d1", 0, 0.9)],
                "q1_sparse": [_make_result("d1", 0, 0.8)],
            }
        )
        assert len(fused) == 1
        assert fused[0]["dense_score"] == 0.9
        assert fused[0]["sparse_score"] == 0.8
        # 同一 chunk 命中两个通道，rrf_score 累加（1/61 + 1/61）
        assert fused[0]["rrf_score"] == pytest.approx(2.0 / 61)

    def test_cross_query_dedup(self):
        """同一 chunk 在多个查询通道命中时去重且分数累加。"""
        fused = reciprocal_rank_fusion(
            {
                "q0_dense": [_make_result("d1", 0, 0.9), _make_result("d2", 0, 0.7)],
                "q1_dense": [_make_result("d1", 0, 0.85)],
            }
        )
        assert len(fused) == 2
        # d1 命中两个查询，融合分更高，排第一
        assert fused[0]["document_id"] == "d1"


class TestSearchHybridMulti:
    """search_hybrid_multi 多查询并行检索。"""

    def _make_service(self):
        service = MilvusService()
        return service

    async def test_parallel_channels_and_embedding_reuse(self, monkeypatch):
        """N 个查询并行发起 N 次 dense/sparse；仅首查询复用预计算向量。"""
        service = self._make_service()
        calls = []

        async def fake_dense(query, k=3, document_ids=None, kb_ids=None, query_embedding=None, source_kind=None):
            calls.append(("dense", query, query_embedding))
            return [_make_result("d", len(calls), 0.9)]

        async def fake_sparse(query, k=3, document_ids=None, kb_ids=None, source_kind=None):
            calls.append(("sparse", query, None))
            return []

        monkeypatch.setattr(service, "search_dense", fake_dense)
        monkeypatch.setattr(service, "search_sparse", fake_sparse)
        monkeypatch.setattr(settings.processing, "KB_RERANK_ENABLED", False)

        emb = [0.1, 0.2]
        results = await service.search_hybrid_multi(
            ["原始问题", "改写查询"], k=3, query_embedding=emb
        )

        dense_calls = [c for c in calls if c[0] == "dense"]
        sparse_calls = [c for c in calls if c[0] == "sparse"]
        assert len(dense_calls) == 2
        assert len(sparse_calls) == 2
        # 首查询复用向量，其余为 None
        assert dense_calls[0][2] == emb
        assert dense_calls[1][2] is None
        # rerank 关闭：按 rrf 截断，rerank_score=rrf_score
        assert len(results) <= 3
        assert all(r["rerank_score"] == r["rrf_score"] for r in results)

    async def test_rerank_uses_original_question(self, monkeypatch):
        """rerank 语义基准必须是 queries[0]（原始问题），且只调用一次。"""
        service = self._make_service()

        async def fake_dense(query, k=3, document_ids=None, kb_ids=None, query_embedding=None, source_kind=None):
            return [_make_result("d1", 0, 0.9)]

        async def fake_sparse(query, k=3, document_ids=None, kb_ids=None, source_kind=None):
            return []

        rerank_queries = []

        async def fake_rerank(query, results, top_k=5):
            rerank_queries.append(query)
            return [dict(r, rerank_score=0.99) for r in results[:top_k]]

        monkeypatch.setattr(service, "search_dense", fake_dense)
        monkeypatch.setattr(service, "search_sparse", fake_sparse)
        monkeypatch.setattr("src.services.hybrid_search.rerank_results", fake_rerank)

        await service.search_hybrid_multi(["原始问题", "改写A", "改写B"], k=3)

        assert rerank_queries == ["原始问题"]

    async def test_dense_failure_degrades(self, monkeypatch):
        """首查询 dense 通道异常时降级跳过，其余通道照常融合。"""
        service = self._make_service()

        async def fake_dense(query, k=3, document_ids=None, kb_ids=None, query_embedding=None, source_kind=None):
            if query == "原始问题":
                raise RuntimeError("embedding 服务不可用")
            return [_make_result("d1", 0, 0.9)]

        async def fake_sparse(query, k=3, document_ids=None, kb_ids=None, source_kind=None):
            return [_make_result("d2", 0, 0.8)]

        monkeypatch.setattr(service, "search_dense", fake_dense)
        monkeypatch.setattr(service, "search_sparse", fake_sparse)
        monkeypatch.setattr(settings.processing, "KB_RERANK_ENABLED", False)

        results = await service.search_hybrid_multi(["原始问题", "改写A"], k=3)
        doc_ids = {r["document_id"] for r in results}
        assert doc_ids == {"d1", "d2"}

    async def test_empty_queries_returns_empty(self):
        """空查询列表直接返回空结果。"""
        service = self._make_service()
        assert await service.search_hybrid_multi([]) == []
        assert await service.search_hybrid_multi(["", "  "]) == []


class TestRetrieveDocumentsRouting:
    """_retrieve_documents 的多查询/单查询路由。"""

    def _make_rag_chain(self):
        vector_store = VectorStoreManager()
        # stub milvus_service 使 _has_vector_store() 判定为已连接（与 test_rag_chain.py 一致）
        vector_store.milvus_service = object()
        rag_chain = RAGChain(vector_store)
        return rag_chain, vector_store

    async def test_multi_query_routing(self, monkeypatch):
        """开关开启且多查询时走 search_hybrid_multi。"""
        rag_chain, vector_store = self._make_rag_chain()
        routed = []

        async def fake_multi(queries, **kwargs):
            routed.append(("multi", queries))
            return []

        async def fake_hybrid(query, **kwargs):
            routed.append(("hybrid", query))
            return []

        monkeypatch.setattr(vector_store, "search_hybrid_multi", fake_multi)
        monkeypatch.setattr(vector_store, "search_hybrid", fake_hybrid)
        monkeypatch.setattr(settings.processing, "KB_MULTI_QUERY_ENABLED", True)

        await rag_chain._retrieve_documents("q", queries=["q", "q改写"])
        assert routed == [("multi", ["q", "q改写"])]

    async def test_switch_off_uses_single_query(self, monkeypatch):
        """开关关闭时即使传入多查询也走单查询路径。"""
        rag_chain, vector_store = self._make_rag_chain()
        routed = []

        async def fake_multi(queries, **kwargs):
            routed.append("multi")
            return []

        async def fake_hybrid(query, **kwargs):
            routed.append(("hybrid", query))
            return []

        monkeypatch.setattr(vector_store, "search_hybrid_multi", fake_multi)
        monkeypatch.setattr(vector_store, "search_hybrid", fake_hybrid)
        monkeypatch.setattr(settings.processing, "KB_MULTI_QUERY_ENABLED", False)

        await rag_chain._retrieve_documents("q", queries=["q", "q改写"])
        assert routed == [("hybrid", "q")]

    async def test_single_query_list_uses_hybrid(self, monkeypatch):
        """改写结果只有一条（无额外查询）时不走多查询路径。"""
        rag_chain, vector_store = self._make_rag_chain()
        routed = []

        async def fake_multi(queries, **kwargs):
            routed.append("multi")
            return []

        async def fake_hybrid(query, **kwargs):
            routed.append(("hybrid", query))
            return []

        monkeypatch.setattr(vector_store, "search_hybrid_multi", fake_multi)
        monkeypatch.setattr(vector_store, "search_hybrid", fake_hybrid)
        monkeypatch.setattr(settings.processing, "KB_MULTI_QUERY_ENABLED", True)

        await rag_chain._retrieve_documents("q", queries=["q"])
        assert routed == [("hybrid", "q")]

    async def test_multi_query_failure_falls_back_to_dense(self, monkeypatch):
        """多查询检索整体失败时回退 dense 检索。"""
        rag_chain, vector_store = self._make_rag_chain()
        routed = []

        async def fake_multi(queries, **kwargs):
            raise RuntimeError("milvus 不可用")

        async def fake_dense(query, k=3, document_ids=None, kb_ids=None, query_embedding=None, source_kind=None):
            routed.append(("dense", query))
            return []

        monkeypatch.setattr(vector_store, "search_hybrid_multi", fake_multi)
        monkeypatch.setattr(vector_store, "search_dense", fake_dense)
        monkeypatch.setattr(settings.processing, "KB_MULTI_QUERY_ENABLED", True)

        await rag_chain._retrieve_documents("q", queries=["q", "q改写"])
        assert routed == [("dense", "q")]
