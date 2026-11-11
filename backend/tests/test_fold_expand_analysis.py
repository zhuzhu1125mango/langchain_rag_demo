"""邻块回填的边界测试（折叠 + 回填组合验证脚本）。

钉住四件事：不跨文档、顺序稳定、不重复、预算超了是「跳过这一块」而不是「停止回填」。
最后一条最容易写错——写成 break 会让长表格行把后面的短邻块也挡掉。
"""

import os
import sys

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
sys.path.insert(0, os.path.join(BACKEND, "scripts"))

from analyze_fold_expand import expand_with_neighbors  # noqa: E402


def _corpus(rows):
    """rows: [(doc, index, text)] → (by_id, index_map)"""
    by_id, index = {}, {}
    for doc, idx, text in rows:
        cid = f"{doc}#{idx}"
        by_id[cid] = {"id": cid, "document_id": doc, "chunk_index": idx, "content": text}
        index[(doc, idx)] = cid
    return by_id, index


CORPUS = [
    ("a.md", 0, "A0 " + "x" * 40),
    ("a.md", 1, "A1 " + "x" * 40),
    ("a.md", 2, "A2 " + "x" * 40),
    ("b.md", 0, "B0 " + "x" * 40),
]


class TestExpandWithNeighbors:
    def test_pulls_both_neighbors(self):
        by_id, index = _corpus(CORPUS)
        out = expand_with_neighbors(["a.md#1"], index, by_id)
        assert out == ["a.md#1", "a.md#0", "a.md#2"]

    def test_does_not_cross_documents(self):
        by_id, index = _corpus(CORPUS)
        out = expand_with_neighbors(["a.md#2"], index, by_id)
        assert "b.md#0" not in out

    def test_keeps_picked_chunks_first(self):
        """原地块必须排在前面：后续拼上下文时它们才是「主体」，邻块是补充。"""
        by_id, index = _corpus(CORPUS)
        out = expand_with_neighbors(["a.md#1", "b.md#0"], index, by_id)
        assert out[:2] == ["a.md#1", "b.md#0"]

    def test_no_duplicates_when_seeds_are_adjacent(self):
        by_id, index = _corpus(CORPUS)
        out = expand_with_neighbors(["a.md#0", "a.md#1"], index, by_id)
        assert out == ["a.md#0", "a.md#1", "a.md#2"]

    def test_missing_neighbor_is_fine(self):
        by_id, index = _corpus(CORPUS)
        assert expand_with_neighbors(["a.md#0"], index, by_id) == ["a.md#0", "a.md#1"]

    def test_unknown_id_is_ignored(self):
        by_id, index = _corpus(CORPUS)
        assert expand_with_neighbors(["ghost#9"], index, by_id) == ["ghost#9"]

    def test_budget_is_measured_including_seeds(self):
        """预算含原地块：3 块原始文本已经超预算时，不该再加邻居。"""
        by_id, index = _corpus(CORPUS)
        base = len(by_id["a.md#1"]["content"])
        out = expand_with_neighbors(["a.md#1"], index, by_id, cap_chars=base + 10)
        assert out == ["a.md#1"]

    def test_budget_skips_one_chunk_but_keeps_expanding(self):
        """超预算的邻块跳过，不能 break —— 否则一个长块会挡住后面所有短块。"""
        by_id, index = _corpus([
            ("a.md", 0, "短" * 5),
            ("a.md", 1, "中心" * 10),
            ("a.md", 2, "长" * 500),
        ])
        base = len(by_id["a.md#1"]["content"])
        # 预算只够再加 a.md#0（短），不够 a.md#2（长）
        out = expand_with_neighbors(["a.md#1"], index, by_id, cap_chars=base + 20)
        assert out == ["a.md#1", "a.md#0"]

    def test_budget_none_means_no_limit(self):
        by_id, index = _corpus(CORPUS)
        assert len(expand_with_neighbors(["a.md#1"], index, by_id, cap_chars=None)) == 3
