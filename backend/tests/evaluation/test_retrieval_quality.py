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
