"""混合检索与重排序模块单元测试。"""

import pytest
from src.services.hybrid_search import reciprocal_rank_fusion, KBReranker


def test_reciprocal_rank_fusion_basic():
    """测试 RRF 基础融合逻辑。"""
    dense_results = [
        {"document_id": "doc1", "chunk_index": 0, "score": 0.9},
        {"document_id": "doc2", "chunk_index": 0, "score": 0.8},
    ]
    sparse_results = [
        {"document_id": "doc2", "chunk_index": 0, "score": 0.95},
        {"document_id": "doc3", "chunk_index": 0, "score": 0.7},
    ]

    fused = reciprocal_rank_fusion({"dense": dense_results, "sparse": sparse_results}, k=60)

    assert len(fused) == 3
    # doc2 在两个通道都出现，rrf_score 应最高
    assert fused[0]["document_id"] == "doc2"
    assert fused[0]["rrf_score"] == pytest.approx(1 / 62 + 1 / 61, rel=1e-6)


def test_reciprocal_rank_fusion_empty_channels():
    """测试某路检索为空时 RRF 不报错。"""
    dense_results = [
        {"document_id": "doc1", "chunk_index": 0, "score": 0.9},
    ]
    fused = reciprocal_rank_fusion({"dense": dense_results, "sparse": []}, k=60)
    assert len(fused) == 1
    assert fused[0]["document_id"] == "doc1"


@pytest.mark.asyncio
async def test_kbreranker_no_model():
    """测试重排序器在模型不可用时的降级行为。"""
    KBReranker._model_loaded = False
    KBReranker._model_failed = True  # 模拟模型加载失败
    KBReranker._model = None

    reranker = await KBReranker.get_instance()
    results = [
        {"document_id": "doc1", "content": "content1", "rrf_score": 0.8},
        {"document_id": "doc2", "content": "content2", "rrf_score": 0.6},
        {"document_id": "doc3", "content": "content3", "rrf_score": 0.9},
    ]
    ranked = await reranker.rerank("query", results, top_k=2)

    assert len(ranked) == 2
    # 按 rrf_score 排序取前 2
    assert ranked[0]["document_id"] == "doc3"
    assert ranked[1]["document_id"] == "doc1"
    assert "rerank_score" in ranked[0]
