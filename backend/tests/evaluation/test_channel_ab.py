"""检索通道 A/B 脚本的纯逻辑单测（不加载模型、不连 Ollama）。

覆盖：分类指标汇总、余弦排序、RRF 融合、并集重排的去重与截断。
端到端数字由 `scripts/run_channel_ab.py` 手动跑（依赖本机 Ollama 与重排模型，不进 CI）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from run_channel_ab import (  # noqa: E402
    _cache_path,
    arm_hybrid,
    arm_rerank_union,
    cosine_rank,
    hit_at,
    mrr_at,
    recall_at,
    score_arm,
)


def _sample(question, golden, category="policy"):
    return {"question": question, "golden_documents": golden, "category": category}


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------

def test_at_k_helpers():
    assert hit_at(["a", "b"], ["b"], k=2) == 1.0
    assert hit_at(["a", "b", "c"], ["c"], k=2) == 0.0
    assert mrr_at(["a", "b"], ["b"], k=2) == 0.5
    assert recall_at(["a", "b"], ["a", "z"], k=2) == 0.5
    assert hit_at([], ["a"], k=5) == 0.0


def test_score_arm_splits_by_category_and_ignores_no_answer():
    dataset = [
        _sample("q1", ["d1"], "policy"),
        _sample("q2", ["d2"], "colloquial"),
        _sample("q3", ["d3a", "d3b"], "multi_hop"),
        {"question": "q4", "golden_documents": [], "category": "no_answer"},
    ]
    rankings = {
        "q1": ["d1", "dx"],
        "q2": ["dx", "dy"],          # 未命中
        "q3": ["d3a", "dz"],          # 命中一半（recall 0.5）
        "q4": ["dx"],
    }

    result = score_arm(rankings, dataset, top_k=5)

    assert result["num_answerable"] == 3
    assert result["by_category"]["policy"]["hit_rate"] == 1.0
    assert result["by_category"]["colloquial"]["hit_rate"] == 0.0
    assert result["by_category"]["multi_hop"]["hit_rate"] == 1.0
    # 指标在脚本里round到 4 位，直接比 2/3 会被舍入绊倒
    assert abs(result["hit_rate"] - 2 / 3) < 1e-3
    # 多跳题只捞到一篇，recall 平均后是 (1 + 0 + 0.5) / 3
    assert abs(result["recall"] - 0.5) < 1e-3


# ---------------------------------------------------------------------------
# dense：余弦排序
# ---------------------------------------------------------------------------

def test_cosine_rank_orders_by_similarity_and_truncates():
    ids = ["a", "b", "c"]
    docs = [[1.0, 0.0], [0.0, 1.0], [0.7, 0.7]]
    ranked = cosine_rank(docs, [1.0, 0.1], ids, top_k=2)
    assert ranked[0] == "c" or ranked[0] == "a"
    assert ranked[0] in {"a", "c"} and len(ranked) == 2
    # 与查询正交的那篇不该排在前面
    assert ranked.index("b") > 0 if "b" in ranked else True


def test_cosine_rank_handles_zero_vector():
    """零向量不能除出 inf/nan（脏数据 / 空文本的嵌入）。"""
    ranked = cosine_rank([[0.0, 0.0], [1.0, 0.0]], [1.0, 0.0], ["zero", "unit"], top_k=2)
    assert ranked[0] == "unit"


# ---------------------------------------------------------------------------
# 融合
# ---------------------------------------------------------------------------

def test_arm_hybrid_uses_rrf_and_truncates():
    sparse = {"q": ["s1", "s2", "s3"]}
    dense = {"q": ["d1", "s2"]}
    fused = arm_hybrid(sparse, dense, channel_top_k=2)
    assert len(fused["q"]) == 2
    # s2 在两个通道都出现，RRF 下应排第一
    assert fused["q"][0] == "s2"


def test_arm_rerank_union_dedupes_and_respects_limit():
    sparse = {"q": ["a", "b", "c"]}
    dense = {"q": ["b", "d"]}
    seen_batches = []

    def _fake_rerank(question, candidates):
        seen_batches.append(list(candidates))
        return list(reversed(candidates))[:2]

    out = arm_rerank_union(
        sparse, dense, [{"question": "q", "golden_documents": ["a"]}], {}, _fake_rerank, limit=3
    )

    assert seen_batches[0] == ["a", "b", "c"], "并集应按先 sparse 后 dense 去重，且不超过 limit"
    assert out["q"] == ["c", "b"]


# ---------------------------------------------------------------------------
# 嵌入缓存
# ---------------------------------------------------------------------------

def test_embedding_cache_key_is_stable_and_content_sensitive(tmp_path):
    p1 = _cache_path(tmp_path, "bge-m3", ["a", "b"])
    p2 = _cache_path(tmp_path, "bge-m3", ["a", "b"])
    p3 = _cache_path(tmp_path, "bge-m3", ["a", "c"])
    p4 = _cache_path(tmp_path, "other", ["a", "b"])

    assert p1 == p2
    assert p1 != p3, "文本变了必须换 key，否则会拿到旧向量"
    assert p1 != p4, "模型变了必须换 key"
