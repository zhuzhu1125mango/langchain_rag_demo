"""生产探针的两个纯函数（2026-11-10）。

探针的价值全在「测量口径对不对」，而口径最容易错在两处：

1. **答案名次**：取「第一个含事实的片段」，不是「含事实最多的片段」——
   后者会把「排序第 18 名但碰巧命中两个词」算成好名次，掩盖「捞不到」；
2. **邻块回填**：只在**同一文档**内 `chunk_index±1`，绝不能跨文档。
   跨了就是拿别的文档的内容冒充「上下文补全」，覆盖率会虚高。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.probe_prod_stages import best_fact_rank, neighbor_ids  # noqa: E402


def _row(doc, idx, text=""):
    return {"document_id": doc, "chunk_index": idx, "content": text}


class TestBestFactRank:
    def test_returns_first_rank_containing_any_fact(self):
        corpus = {
            "a": _row("d1", 0, "无关内容"),
            "b": _row("d1", 1, "这里提到 intent_router"),
            "c": _row("d1", 2, "这里两个都有 intent_router 和 output_sanitizer"),
        }
        rank, hits = best_fact_rank(["a", "b", "c"], corpus, ["intent_router", "output_sanitizer"])
        assert (rank, hits) == (2, 1)

    def test_returns_none_when_no_fact_found(self):
        corpus = {"a": _row("d1", 0, "无关")}
        assert best_fact_rank(["a"], corpus, ["intent_router"]) == (None, 0)

    def test_ignores_ids_missing_from_corpus(self):
        corpus = {"b": _row("d1", 1, "SIGTERM")}
        assert best_fact_rank(["gone", "b"], corpus, ["SIGTERM"]) == (2, 1)

    def test_empty_facts_yields_none(self):
        corpus = {"a": _row("d1", 0, "任意")}
        assert best_fact_rank(["a"], corpus, []) == (None, 0)


class TestNeighborIds:
    def test_keeps_picked_then_adds_neighbors(self):
        corpus = {
            "c1": _row("doc", 1),
            "c0": _row("doc", 0),
            "c2": _row("doc", 2),
        }
        out = neighbor_ids(["c1"], corpus)
        assert out[0] == "c1"          # 原地优先，回填只追加
        assert set(out) == {"c1", "c0", "c2"}

    def test_does_not_cross_documents(self):
        """别的文档的第 0 块不是「相邻块」，跨过去就是伪造上下文。"""
        corpus = {
            "a0": _row("docA", 0),
            "b0": _row("docB", 0),
        }
        assert neighbor_ids(["a0"], corpus) == ["a0"]

    def test_dedupes_when_two_picked_share_a_neighbor(self):
        corpus = {
            "c0": _row("doc", 0),
            "c1": _row("doc", 1),
            "c2": _row("doc", 2),
        }
        out = neighbor_ids(["c0", "c1"], corpus)
        assert len(out) == len(set(out)) == 3
        assert set(out) == {"c0", "c1", "c2"}

    def test_unknown_picked_id_is_skipped(self):
        corpus = {"c0": _row("doc", 0)}
        assert neighbor_ids(["nope"], corpus) == ["nope"]
