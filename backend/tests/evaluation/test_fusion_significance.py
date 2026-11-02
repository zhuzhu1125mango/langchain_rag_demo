"""融合策略与显著性检验的单元测试。

只测**逻辑正确性**，不测性能也不断言具体的评测分数——分数会随语料变，
锁死数字会让测试变成"改语料就红"的负担。重点守住三件事：

1. 加权 RRF 在等权时必须**退化回**标准 RRF（否则两个实现会悄悄不一致）；
2. 分数融合在权重取极值时退化为单通道排序；
3. McNemar 的 p 值与手算一致（这是所有结论的判据，算错了整份报告就错了）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

from ab_significance import compare, mcnemar_exact  # noqa: E402
from run_channel_ab import minmax_fuse, weighted_rrf_fuse  # noqa: E402
from run_multiquery_ab import rrf_fuse  # noqa: E402


class TestWeightedRRF:
    def test_equal_weight_matches_standard_rrf(self):
        """w=0.5 必须与 rrf_fuse 完全一致——两个实现不能各说各话。"""
        sparse = ["a", "b", "c", "d"]
        dense = ["c", "a", "e", "b"]

        expected = rrf_fuse([sparse, dense])
        actual = weighted_rrf_fuse(sparse, dense, w_dense=0.5, top_k=10)
        assert actual == expected

    def test_full_dense_weight_follows_dense_order(self):
        """w=1.0 时稀疏通道权重为 0，排序应以 dense 名次为准。"""
        sparse = ["s1", "s2"]
        dense = ["d1", "d2", "d3"]

        assert weighted_rrf_fuse(sparse, dense, w_dense=1.0, top_k=3) == ["d1", "d2", "d3"]

    def test_zero_dense_weight_follows_sparse_order(self):
        sparse = ["s1", "s2", "s3"]
        dense = ["d1", "d2"]

        assert weighted_rrf_fuse(sparse, dense, w_dense=0.0, top_k=3) == ["s1", "s2", "s3"]

    def test_truncates_to_top_k(self):
        result = weighted_rrf_fuse(["a", "b", "c"], ["c", "b", "a"], w_dense=0.5, top_k=2)
        assert len(result) == 2

    def test_document_present_in_both_channels_ranks_higher(self):
        """两路都排第一的文档，应高于只在一路排第一的文档（共识加分）。"""
        sparse = ["both", "only_s"]
        dense = ["both", "only_d"]

        result = weighted_rrf_fuse(sparse, dense, w_dense=0.5, top_k=3)
        assert result[0] == "both"


class TestMinMaxFuse:
    def test_full_dense_weight_ignores_sparse_scores(self):
        """w=1.0 时稀疏的归一化分数被乘 0，排序等于 dense 自己的分数序。"""
        sparse = [("s1", 9.0), ("s2", 1.0)]
        dense = [("d1", 0.9), ("d2", 0.5), ("d3", 0.1)]

        assert minmax_fuse(sparse, dense, w_dense=1.0, top_k=3) == ["d1", "d2", "d3"]

    def test_zero_dense_weight_ignores_dense_scores(self):
        sparse = [("s1", 9.0), ("s2", 1.0)]
        dense = [("d1", 0.9), ("d2", 0.5)]

        assert minmax_fuse(sparse, dense, w_dense=0.0, top_k=2) == ["s1", "s2"]

    def test_equal_scores_do_not_divide_by_zero(self):
        """两路分数全相等时 min-max 分母为 0，不能崩，也不能给出全 0 分。"""
        sparse = [("s1", 1.0), ("s2", 1.0)]
        dense = [("d1", 0.5), ("d2", 0.5)]

        result = minmax_fuse(sparse, dense, w_dense=0.5, top_k=4)
        assert set(result) == {"s1", "s2", "d1", "d2"}

    def test_bm25_unbounded_scores_are_comparable_after_normalisation(self):
        """BM25 分数无上界（这里到 12.0），余弦在 [0,1]，归一化后必须可比。"""
        sparse = [("s1", 12.0), ("s2", 2.0)]
        dense = [("d1", 0.95), ("d2", 0.90)]

        # dense 权重 0.5：s1 与 d1 都是各自通道的第一名，归一化后同为 1.0
        result = minmax_fuse(sparse, dense, w_dense=0.5, top_k=4)
        assert set(result[:2]) == {"s1", "d1"}


class TestMcNemar:
    @pytest.mark.parametrize(
        "b, c, expected",
        [
            (0, 0, 1.0),  # 两臂完全一致，无信息
            (6, 0, 0.03125),  # 2 * 0.5^6 —— 达到显著的门槛
            (5, 0, 0.0625),  # 2 * 0.5^5 —— 差一点
            (3, 0, 0.25),
            (1, 0, 1.0),  # 单题翻转，p 被截到 1.0
            (3, 3, 1.0),  # 对称，双向打平
        ],
    )
    def test_known_values(self, b, c, expected):
        assert mcnemar_exact(b, c) == pytest.approx(expected)

    def test_symmetric_in_argument_order(self):
        """检验是双侧的，方向不能影响 p 值。"""
        assert mcnemar_exact(7, 2) == pytest.approx(mcnemar_exact(2, 7))

    def test_p_value_never_exceeds_one(self):
        assert 0.0 <= mcnemar_exact(1, 0) <= 1.0


class TestCompare:
    def test_only_counts_questions_present_in_both_arms(self):
        """数据集版本不一致时，不能把「某臂没跑这道题」算成未命中。"""
        a = {"q1": True, "q2": True}
        b = {"q1": True, "q2": False, "q3": True}

        result = compare(a, b, "a", "b")
        assert result["n"] == 2
        assert result["both"] == 1
        assert result["only_a"] == ["q2"]
        assert result["only_b"] == []

    def test_reports_delta_in_percentage_points(self):
        a = {f"q{i}": True for i in range(10)}
        b = {f"q{i}": (i < 5) for i in range(10)}  # b 少命中 5 题

        result = compare(a, b, "a", "b")
        assert result["delta_pp"] == pytest.approx(-50.0)
        assert result["one_question_pp"] == pytest.approx(10.0)

    def test_single_flip_is_not_significant(self):
        """这正是本次实验的教训：1 题翻转绝不能当结论。"""
        a = {f"q{i}": True for i in range(64)}
        b = dict(a)
        b["q0"] = False

        result = compare(a, b, "a", "b")
        assert result["p_value"] == 1.0
