"""Wiki 跨文档多跳 A/B 评估逻辑的单测（全离线，纳入 CI unit job）。

校验 run_wiki_multihop_ab.py 的溯源指标与决策建议逻辑正确、数据集合法。
编译环节需本地 Ollama，不在单测内执行（e2e 由脚本手动运行）。

与 scripts/run_eval.py 体系保持一致（test_retrieval_quality.py / test_multiquery_ab.py）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from run_eval import load_jsonl  # noqa: E402
from run_wiki_multihop_ab import (  # noqa: E402
    DEFAULT_CORPUS,
    DEFAULT_DATASET,
    build_link_graph,
    d_recommend,
    metrics_with_provenance,
    recommend,
    wiki_entry_id,
    wiki_nav_ranked,
)


def test_multihop_dataset_valid():
    """数据集合法：每个样本都是真跨文档问题（golden ≥2 且全部存在于语料）。"""
    dataset = load_jsonl(DEFAULT_DATASET)
    corpus = load_jsonl(DEFAULT_CORPUS)
    assert dataset, "多跳评测数据集为空"
    assert corpus, "评测语料为空"
    corpus_ids = {d["id"] for d in corpus}
    questions = set()
    for sample in dataset:
        golden = sample.get("golden_documents", [])
        assert len(golden) >= 2, f"样本不是跨文档问题（golden 少于 2）: {sample.get('question')}"
        assert set(golden) <= corpus_ids, f"golden 文档不在语料中: {golden}"
        assert sample["question"] not in questions, f"问题重复: {sample['question']}"
        questions.add(sample["question"])


def test_wiki_entry_id_contains_source_doc():
    """wiki 条目 id 必须携带来源 doc_id，避免跨文档页标题冲突。"""
    assert wiki_entry_id("doc_a", "考勤") == "wiki::doc_a::考勤"
    assert wiki_entry_id("doc_a", "考勤") != wiki_entry_id("doc_b", "考勤")


def test_metrics_with_provenance_maps_wiki_hits():
    """wiki 页命中按溯源折算为来源文档命中，golden 部分命中计 recall 0.5。"""
    provenance = {"wiki::d1::页A": "d1", "wiki::d2::页B": "d2"}
    details = [
        {
            "question": "q",
            "golden_documents": ["d1", "d2"],
            "ranked_top_k": ["wiki::d1::页A", "noise", "d3", "wiki::d2::页B", "d4"],
        },
        {
            "question": "q2",
            "golden_documents": ["d1", "d2"],
            "ranked_top_k": ["d3", "d4", "d5", "d6", "d7"],
        },
    ]
    m = metrics_with_provenance(details, provenance, top_k=5)
    # 样本1：d1 在 rank1、d2 在 rank4 → hit=1, mrr=1.0, recall=1.0
    # 样本2：全 miss → hit=0, mrr=0, recall=0
    assert m["hit_rate"] == 0.5
    assert m["mrr"] == 0.5
    assert m["recall"] == 0.5


def test_recommend_gain_branch():
    """B 臂有明确增益 → 建议降级 P2-1。"""
    a = {"hit_rate": 0.5, "mrr": 0.4, "recall": 0.5}
    b = {"hit_rate": 0.75, "mrr": 0.5, "recall": 0.625}
    wiki_only = {"hit_rate": 0.3, "mrr": 0.2, "recall": 0.2}
    text = recommend(a, b, wiki_only)
    assert "降级" in text or "合并" in text


def test_recommend_no_gain_branch():
    """B 臂无增益且 wiki 自身召回低 → 建议 GraphRAG 立项。"""
    a = {"hit_rate": 0.625, "mrr": 0.4, "recall": 0.5}
    b = {"hit_rate": 0.625, "mrr": 0.4, "recall": 0.5}
    wiki_only = {"hit_rate": 0.1, "mrr": 0.05, "recall": 0.1}
    text = recommend(a, b, wiki_only)
    assert "GraphRAG" in text


def test_build_link_graph_and_nav_ranked():
    """D 臂：链接图按 [[Title]] 构建，导航排序 = 入口页在前 + 邻居页按序追加。"""
    entries = [
        {"id": "p1", "title": "考勤管理", "text": "考勤规则，见[[远程办公]]与[[请假制度]]。"},
        {"id": "p2", "title": "远程办公", "text": "远程规范。"},
        {"id": "p3", "title": "请假制度", "text": "请假流程。"},
        {"id": "p4", "title": "无关页", "text": "其他内容。"},
    ]
    graph = build_link_graph(entries)
    # p1 的邻居按正文出现顺序去重
    assert [n["id"] for n in graph["p1"]] == ["p2", "p3"]
    assert graph["p2"] == []

    def fake_bm25(question, top_k):
        # 无关页排在最前，导航不应受其影响（入口取 2 页后沿链接扩展）
        return ["p4", "p1", "p2", "p3"][:top_k]

    ranked = wiki_nav_ranked(fake_bm25, entries, graph, "考勤与远程办公", top_k=5)
    assert ranked[0] == "p4"
    assert ranked[1] == "p1"
    # 邻居页紧随入口页进入 top-k
    assert set(ranked[2:]) >= {"p2", "p3"}


def test_d_recommend_branches():
    """D 臂判定三分支：达标成立 / 持平保守 / 证伪回退。"""
    a = {"hit_rate": 1.0, "mrr": 1.0, "recall": 0.938}
    b = {"hit_rate": 1.0, "mrr": 1.0, "recall": 0.875}
    ok = {"hit_rate": 1.0, "mrr": 1.0, "recall": 0.938}
    bad = {"hit_rate": 1.0, "mrr": 1.0, "recall": 0.75}
    assert "立项依据成立" in d_recommend(a, b, ok)
    # 持平分支：D == 基线但混入臂更高（罕见），保守观察
    b_high = {"hit_rate": 1.0, "mrr": 1.0, "recall": 0.95}
    assert "灰度观察" in d_recommend(a, b_high, ok)
    assert "回退" in d_recommend(a, b, bad)
