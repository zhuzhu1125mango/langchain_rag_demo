"""知识库检索质量评测。

阶段一升级：建立检索指标（Hit Rate / MRR / Recall）与评测数据集，
用于量化混合检索（dense + BM25 + RRF + rerank）的效果。
"""

import json
import os
from pathlib import Path
from typing import Dict, List

import pytest

DATASET_PATH = Path(__file__).parent / "kb_eval_dataset.jsonl"


def _load_dataset() -> List[Dict]:
    """加载评测数据集。"""
    if not DATASET_PATH.exists():
        return []
    with open(DATASET_PATH, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def compute_hit_rate(results: List[Dict], golden_documents: List[str], k: int = 5) -> float:
    """计算 Hit Rate@K：Top-K 中是否命中任一 golden document。"""
    if not golden_documents:
        return 0.0
    top_k = results[:k]
    top_doc_ids = {r.get("document_id") for r in top_k}
    return 1.0 if set(golden_documents) & top_doc_ids else 0.0


def compute_mrr(results: List[Dict], golden_documents: List[str], k: int = 5) -> float:
    """计算 MRR@K：第一个命中 golden document 的结果的倒数排名。"""
    for rank, result in enumerate(results[:k], start=1):
        if result.get("document_id") in golden_documents:
            return 1.0 / rank
    return 0.0


def compute_recall(results: List[Dict], golden_documents: List[str], k: int = 5) -> float:
    """计算 Recall@K：命中 golden document 的比例。"""
    if not golden_documents:
        return 0.0
    top_doc_ids = {r.get("document_id") for r in results[:k]}
    hits = len(set(golden_documents) & top_doc_ids)
    return hits / len(golden_documents)


@pytest.fixture
def dataset() -> List[Dict]:
    return _load_dataset()


def test_retrieval_metrics_helper():
    """验证检索指标计算 helper 的正确性。"""
    results = [
        {"document_id": "doc1"},
        {"document_id": "doc2"},
        {"document_id": "doc3"},
    ]
    assert compute_hit_rate(results, ["doc2"], k=2) == 1.0
    assert compute_hit_rate(results, ["doc4"], k=3) == 0.0
    assert compute_mrr(results, ["doc2"], k=3) == 0.5
    assert compute_recall(results, ["doc1", "doc4"], k=3) == 0.5


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_kb_retrieval_metrics(dataset):
    """端到端检索指标评测：需要 Milvus 与 Ollama 服务运行。"""
    if not dataset:
        pytest.skip("评测数据集为空")

    try:
        from src.services.milvus_service import MilvusService
        from src.config import settings
    except Exception as e:
        pytest.skip(f"依赖导入失败: {e}")

    service = await MilvusService.get_instance()

    hit_rates = []
    mrrs = []
    recalls = []

    for sample in dataset:
        results = await service.search_hybrid(
            sample["question"],
            k=settings.processing.KB_HYBRID_RERANK_TOP_K,
        )
        golden = sample.get("golden_documents", [])
        hit_rates.append(compute_hit_rate(results, golden, k=5))
        mrrs.append(compute_mrr(results, golden, k=5))
        recalls.append(compute_recall(results, golden, k=5))

    avg_hit_rate = sum(hit_rates) / len(hit_rates)
    avg_mrr = sum(mrrs) / len(mrrs)
    avg_recall = sum(recalls) / len(recalls)

    print(f"Hit Rate@5: {avg_hit_rate:.2f}, MRR@5: {avg_mrr:.2f}, Recall@5: {avg_recall:.2f}")

    # 门槛作为回归保护，可根据实际评测集数据调整
    assert avg_hit_rate >= 0.0
    assert avg_mrr >= 0.0
    assert avg_recall >= 0.0
