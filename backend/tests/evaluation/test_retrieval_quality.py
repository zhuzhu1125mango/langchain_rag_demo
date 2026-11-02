"""检索质量回归测试（全离线，纳入 CI unit job）。

复用 scripts/run_eval.py 的评估逻辑与阈值：基于评估数据集与语料
运行 BM25 检索，断言汇总指标达到阈值。阈值可通过环境变量覆盖
（EVAL_MIN_HIT_RATE / EVAL_MIN_MRR / EVAL_MIN_RECALL），与 CI
rag-eval job 的卡点保持一致。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from run_eval import (  # noqa: E402
    DEFAULT_CORPUS,
    DEFAULT_DATASET,
    compute_hit_rate,
    compute_mrr,
    compute_recall,
    evaluate,
    get_thresholds,
    load_jsonl,
)


def test_retrieval_quality_thresholds():
    """检索指标不得低于阈值（人为降低检索质量时应失败）。"""
    dataset = load_jsonl(DEFAULT_DATASET)
    corpus = load_jsonl(DEFAULT_CORPUS)
    assert dataset, "评测数据集为空"
    assert corpus, "评测语料为空"

    result = evaluate(dataset, corpus, top_k=5)
    metrics = result["metrics"]

    for name, threshold in get_thresholds().items():
        assert metrics[name] >= threshold, (
            f"检索质量回归: {name}={metrics[name]:.3f} < 阈值 {threshold}"
        )


def test_eval_dataset_composition():
    """数据集本身的守卫：别让人悄悄缩回 5 题。

    2026-11-02 之前是 5 题 / 12 篇，hit_rate 恒为 1.0——指标再绿也发现不了退化。
    这里固定住「有规模、有难例、golden 不出圈」三条底线。
    """
    dataset = load_jsonl(DEFAULT_DATASET)
    corpus = load_jsonl(DEFAULT_CORPUS)

    assert len(dataset) >= 40, f"题目太少（{len(dataset)}），指标会失去区分度"
    assert len(corpus) >= 50, f"语料太少（{len(corpus)}），Top-5 命中会虚高"

    # 题型覆盖：精确匹配类来自生产真实查询（型号 + 字段），是流量主力；
    # 没有它，「要不要保留稀疏通道」这类决策就是在错误分布上得出的。
    categories = {q.get("category") for q in dataset}
    for required in ("policy", "colloquial", "exact_match"):
        assert required in categories, f"评测集缺少 {required} 类题目"
    assert (DEFAULT_CORPUS.exists())
    assert any(len(q.get("golden_documents", [])) > 1 for q in dataset), "缺少多跳题（golden 两篇）"
    assert any(not q.get("golden_documents") for q in dataset), "缺少无答案题"

    corpus_ids = {doc["id"] for doc in corpus}
    for sample in dataset:
        for golden in sample.get("golden_documents", []):
            assert golden in corpus_ids, f"golden 不在语料中: {golden}（题：{sample['question']}）"


def test_metrics_split_answerable_and_no_answer():
    """无答案题不得并入 hit/mrr/recall 的均值（否则召回率与拒答率被混成一个数）。"""
    dataset = load_jsonl(DEFAULT_DATASET)
    corpus = load_jsonl(DEFAULT_CORPUS)
    metrics = evaluate(dataset, corpus, top_k=5)["metrics"]

    assert metrics["num_answerable"] + metrics["num_no_answer"] == metrics["num_questions"]
    assert metrics["num_no_answer"] > 0
    assert "no_answer_separation" in metrics
    # 分离度是比值，语料或题目变化时会漂移，这里只守住它是个有限数
    assert isinstance(metrics["no_answer_separation"], float)


def test_retrieval_metrics_helper():
    """指标计算 helper 的边界行为。"""
    results = ["doc1", "doc2", "doc3"]
    assert compute_hit_rate(results, ["doc2"], k=2) == 1.0
    assert compute_hit_rate(results, ["doc4"], k=3) == 0.0
    assert compute_mrr(results, ["doc2"], k=3) == 0.5
    assert compute_recall(results, ["doc1", "doc4"], k=3) == 0.5
    assert compute_hit_rate(results, [], k=3) == 0.0


@pytest.mark.parametrize(
    ("env_name", "metric_key"),
    [
        ("EVAL_MIN_HIT_RATE", "hit_rate"),
        ("EVAL_MIN_MRR", "mrr"),
        ("EVAL_MIN_RECALL", "recall"),
    ],
)
def test_threshold_env_override(env_name, metric_key, monkeypatch):
    """阈值环境变量应能覆盖默认值（验证 CI 阈值配置生效途径）。"""
    monkeypatch.setenv(env_name, "0.99")
    assert get_thresholds()[metric_key] == 0.99
