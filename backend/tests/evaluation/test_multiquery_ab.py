"""多查询 A/B 评估逻辑的单测（全离线，纳入 CI unit job）。

校验 run_multiquery_ab.py 的 RRF 融合与双臂评估结构正确性。
注意：本脚本是"开关决策工具"，不做增益阈值卡点——只断言逻辑正确、
数据集合法、A 臂（单查询基线）能满足标准检索阈值（回归共识）。

与 scripts/run_eval.py 的体系保持一致（test_retrieval_quality.py）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from run_multiquery_ab import (  # noqa: E402
    CHANNEL_TOP_K,
    DEFAULT_CORPUS,
    DEFAULT_DATASET,
    RRF_K,
    evaluate_arms,
    recommend,
    rrf_fuse,
)
from run_eval import (  # noqa: E402
    get_thresholds,
    load_jsonl,
)


def test_rrf_fuse_math():
    """RRF 融合：跨通道共识文档累加分数，与生产口径一致（k=RRF_K=60）。"""
    fused = rrf_fuse(
        [["a", "b", "c"], ["a", "d", "b"]],
        k=RRF_K,
    )
    # a 两通道都排 rank1，累加分最高；b 两通道都出现（rank2/3）次之；
    # c（rank3）、d（rank2）仅单通道，分数更低
    assert fused[0] == "a"
    assert fused[1] == "b"
    assert set(fused[:4]) == {"a", "b", "c", "d"}


def test_rrf_fuse_single_channel_top_rank_not_suppressed():
    """仅单通道顶排名的文档仍能被检索到（k 参与平滑，避免过平滑稀释）。"""
    # a 只在 ch0 rank1，b 在 ch1 低排多次出现；k 越小 rank1 的权重占比越大
    fused = rrf_fuse([["a"], ["b", "c", "d", "e", "f", "g"]], k=1)
    assert fused[0] == "a"


def test_rrf_fuse_empty_channels():
    """空通道安全，不抛异常且结果为空。"""
    assert rrf_fuse([[], []], k=60) == []


def test_multiquery_ab_dataset_valid():
    """数据集合法：每个样本都有非空 query_variants。"""
    dataset = load_jsonl(DEFAULT_DATASET)
    corpus = load_jsonl(DEFAULT_CORPUS)
    assert dataset, "评测数据集为空"
    assert corpus, "评测语料为空"
    for sample in dataset:
        assert sample.get("query_variants"), f"样本缺少 query_variants: {sample.get('question')}"


def test_arms_report_structure_and_a_baseline():
    """A/B 双臂评估结构正确，且 A 臂（单查询）满足标准回归阈值。"""
    dataset = load_jsonl(DEFAULT_DATASET)
    corpus = load_jsonl(DEFAULT_CORPUS)
    result = evaluate_arms(dataset, corpus, top_k=5)

    assert result["num_questions"] == len(dataset)
    assert result["top_k"] == 5
    assert result["channel_top_k"] == CHANNEL_TOP_K
    assert result["rrf_k"] == RRF_K
    assert set(result) >= {"arm_A_single", "arm_B_multiquery"}
    assert len(result["details"]) == len(dataset)

    a = result["arm_A_single"]
    b = result["arm_B_multiquery"]
    for metric in ("hit_rate", "mrr", "recall"):
        assert 0.0 <= a[metric] <= 1.0
        assert 0.0 <= b[metric] <= 1.0

    # A 臂是生产当用的单查询基线，须满足标准检索阈值（与 CI rag-eval 同口径）
    for name, threshold in get_thresholds().items():
        assert a[name] >= threshold, f"A 臂单查询回归: {name}={a[name]:.3f} < {threshold}"


def test_recommend_informative():
    """决策建议无论开 / 关都返回可读结论。"""
    a = {"hit_rate": 0.9, "recall": 0.9}
    b_no_gain = {"hit_rate": 0.9, "recall": 0.9}
    b_gain = {"hit_rate": 1.0, "recall": 1.0}
    close_msg = recommend({"arm_A_single": a, "arm_B_multiquery": b_no_gain})
    open_msg = recommend({"arm_A_single": a, "arm_B_multiquery": b_gain})
    assert "关闭" in close_msg
    assert "开启" in open_msg