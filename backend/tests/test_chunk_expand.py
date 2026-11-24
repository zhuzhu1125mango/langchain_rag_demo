"""邻块回填的边界测试。

钉住四件事，因为这四条一旦破了，症状都是「上下文看起来正常但答案变差」，
在覆盖率指标上不一定看得出来：

1. 不跨文档 —— 从别的文档补块是纯粹的噪声；
2. 不重复 —— 相邻的两个原地块互为邻块时，不能补出重复块；
3. 不超预算 —— 超预算跳过继续看下一个，而不是整批放弃；
4. 顺序稳定 —— 原地块在前、邻块按「原地块顺序 → 先 -1 后 +1」，否则重排/引用错位。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.chunk_expand import (  # noqa: E402
    merge_appended,
    merge_with_neighbors,
    neighbor_positions,
    same_heading_as_parent,
    section_prefix,
    section_prefixes,
)


def _row(doc, idx, text="x" * 10, cid=None, heading=""):
    return {
        "id": cid or f"{doc}#{idx}",
        "document_id": doc,
        "chunk_index": idx,
        "content": text,
        "heading_path": heading,
    }


class TestNeighborPositions:
    def test_asks_for_both_sides(self):
        assert neighbor_positions([_row("d1", 5)]) == [("d1", 4), ("d1", 6)]

    def test_negative_index_is_dropped(self):
        """第 0 块的 -1 不存在，不能发出去（否则白查一次，且表达式里出现 -1）。"""
        assert neighbor_positions([_row("d1", 0)]) == [("d1", 1)]

    def test_dedupes_shared_neighbor(self):
        """两块相邻时，中间那块会被两边都算作邻块，只查一次。"""
        got = neighbor_positions([_row("d1", 5), _row("d1", 6)])
        assert got == [("d1", 4), ("d1", 6), ("d1", 5), ("d1", 7)]

    def test_never_crosses_documents(self):
        got = neighbor_positions([_row("d1", 3), _row("d2", 3)])
        assert ("d1", 2) in got and ("d2", 2) in got
        assert all(doc in ("d1", "d2") for doc, _ in got)

    def test_skips_rows_without_position(self):
        assert neighbor_positions([{"id": "x", "content": "c"}]) == []


class TestSameHeadingGate:
    """回填只补同章节邻块：物理相邻不等于语义连续。"""

    def test_cross_heading_neighbor_is_dropped(self):
        base = [_row("d1", 5, heading="部署 > 7.3 监控指标")]
        nbs = [_row("d1", 6, heading="部署 > 7.4 告警规则")]
        assert same_heading_as_parent(nbs, base) == []

    def test_same_heading_neighbor_is_kept(self):
        base = [_row("d1", 5, heading="部署 > 7.3 监控指标")]
        nbs = [_row("d1", 6, heading="部署 > 7.3 监控指标")]
        assert len(same_heading_as_parent(nbs, base)) == 1

    def test_unknown_heading_on_either_side_is_kept(self):
        """任一侧没有 heading 时无从判断，宁可保留（否则没 heading 的文档永不回填）。"""
        assert len(same_heading_as_parent([_row("d1", 6, heading="")], [_row("d1", 5)])) == 1
        assert len(same_heading_as_parent([_row("d1", 6, heading="A")], [_row("d1", 5, heading="")])) == 1

    def test_neighbor_between_two_parents_kept_if_either_matches(self):
        """6 既是 5 的下邻又是 7 的上邻：只要跟其中一个同章节就保留。"""
        base = [_row("d1", 5, heading="A"), _row("d1", 7, heading="B")]
        nbs = [_row("d1", 6, heading="B")]
        assert len(same_heading_as_parent(nbs, base)) == 1

    def test_prefix_is_not_enough(self):
        """`7.3` 与 `7.4` 的前缀都是 `7`：不做前缀放宽，否则闸门形同虚设。"""
        base = [_row("d1", 5, heading="部署 > 7.3 监控指标")]
        nbs = [_row("d1", 6, heading="部署 > 7.31 指标细则")]
        assert same_heading_as_parent(nbs, base) == []

    def test_rows_without_position_are_dropped(self):
        assert same_heading_as_parent([{"id": "x", "content": "c", "heading_path": "A"}], [_row("d1", 5, heading="A")]) == []


class TestMergeWithNeighbors:
    def test_base_order_preserved_and_neighbors_appended(self):
        base = [_row("d1", 5, "orig5"), _row("d1", 9, "orig9")]
        nbs = [_row("d1", 8, "n8"), _row("d1", 4, "n4"), _row("d1", 10, "n10"), _row("d1", 6, "n6")]
        out = merge_with_neighbors(base, nbs)
        assert [r["id"] for r in out] == ["d1#5", "d1#9", "d1#4", "d1#6", "d1#8", "d1#10"]

    def test_does_not_duplicate_a_base_chunk_that_is_also_a_neighbor(self):
        """5 和 6 都被选中，6 是 5 的右邻块——不能再补一份。"""
        base = [_row("d1", 5), _row("d1", 6)]
        nbs = [_row("d1", 6), _row("d1", 7)]
        out = merge_with_neighbors(base, nbs)
        ids = [r["id"] for r in out]
        assert len(ids) == len(set(ids))
        assert ids == ["d1#5", "d1#6", "d1#7"]

    def test_never_crosses_documents(self):
        """补查表达式是两个 in 的笛卡尔积，可能带回别的文档的块；这里必须挡住。"""
        base = [_row("d1", 5)]
        nbs = [_row("d2", 4, "其他文档"), _row("d1", 6, "本该补的")]
        out = merge_with_neighbors(base, nbs)
        assert [r["id"] for r in out] == ["d1#5", "d1#6"]

    def test_budget_counts_base_and_skips_oversized_neighbor(self):
        base = [_row("d1", 5, "a" * 100)]
        nbs = [_row("d1", 4, "b" * 900), _row("d1", 6, "c" * 50)]
        out = merge_with_neighbors(base, nbs, budget_chars=200)
        # 900 那个超预算被跳过，但 50 那个仍然补上——预算不是「整批放弃」
        assert [r["id"] for r in out] == ["d1#5", "d1#6"]

    def test_base_never_dropped_even_if_over_budget(self):
        """原地块本身已超预算时，仍然全部保留：预算只约束「补不补」。"""
        base = [_row("d1", 5, "a" * 500)]
        out = merge_with_neighbors(base, [_row("d1", 6, "b" * 10)], budget_chars=100)
        assert [r["id"] for r in out] == ["d1#5"]

    def test_none_budget_means_unlimited(self):
        base = [_row("d1", 5, "a" * 500)]
        out = merge_with_neighbors(base, [_row("d1", 6, "b" * 10)], budget_chars=None)
        assert len(out) == 2

    def test_only_one_level(self):
        """回填不递归：邻块的邻块不会因为「邻块自己也想要邻居」被带进来。"""
        base = [_row("d1", 5)]
        nbs = [_row("d1", 4), _row("d1", 3), _row("d1", 6), _row("d1", 7)]
        out = merge_with_neighbors(base, nbs)
        assert [r["id"] for r in out] == ["d1#5", "d1#4", "d1#6"]

    def test_missing_neighbor_is_simply_absent(self):
        """文档末尾的块只有左邻块，右邻块查不到——不能因此中断。"""
        out = merge_with_neighbors([_row("d1", 99)], [_row("d1", 98)])
        assert [r["id"] for r in out] == ["d1#99", "d1#98"]

    def test_inputs_not_mutated(self):
        base = [_row("d1", 5)]
        nbs = [_row("d1", 6)]
        merge_with_neighbors(base, nbs)
        assert len(base) == 1 and len(nbs) == 1


class TestSectionPrefix:
    # 整节回填的取数边界：算错前缀 = 把别的章节或整篇文档塞进上下文
    H1 = "备份 / 恢复 / 回滚与容量 Runbook（SEC-13 / OPS-1）"

    def test_three_levels_returns_parent_section(self):
        path = f"{self.H1} > 4. 恢复 > 4.1 恢复顺序（不可打乱）"
        assert section_prefix(path) == f"{self.H1} > 4. 恢复"

    def test_sibling_subsection_maps_to_same_prefix(self):
        """4.1 与 4.2 必须归到同一个前缀，否则「选中 4.1」带不出 4.2。"""
        a = section_prefix(f"{self.H1} > 4. 恢复 > 4.1 恢复顺序")
        b = section_prefix(f"{self.H1} > 4. 恢复 > 4.2 恢复后必做")
        assert a == b == f"{self.H1} > 4. 恢复"

    def test_two_levels_is_refused(self):
        """只有两级时去掉最后一段会退化成一级标题 = 整篇文档，必须拒绝。"""
        assert section_prefix(f"{self.H1} > 4. 恢复") == ""

    def test_single_level_is_refused(self):
        assert section_prefix(self.H1) == ""

    def test_empty_heading_is_refused(self):
        assert section_prefix("") == ""
        assert section_prefix(None) == ""

    def test_extra_whitespace_tolerated(self):
        path = f"  {self.H1}   >   4. 恢复   >   4.1 恢复顺序  "
        assert section_prefix(path) == f"{self.H1} > 4. 恢复"


class TestSectionPrefixes:
    def test_dedupes_and_keeps_first_seen_order(self):
        rows = [
            _row("d1", 25, heading="A > 4. 恢复 > 4.1 顺序"),
            _row("d2", 7, heading="B > 2. 备份 > 2.1 全量"),
            _row("d1", 26, heading="A > 4. 恢复 > 4.1 顺序"),
        ]
        assert section_prefixes(rows) == [("d1", "A > 4. 恢复"), ("d2", "B > 2. 备份")]

    def test_rows_without_prefix_are_skipped(self):
        rows = [
            _row("d1", 1, heading="A > 1. 概览"),
            _row("d1", 2, heading=""),
        ]
        assert section_prefixes(rows) == []


class TestMergeAppended:
    def test_appends_in_given_order(self):
        """整节回填按 chunk_index 升序追加，让模型读到完整正文而不是打散的块。"""
        base = [_row("d1", 25)]
        extras = [_row("d1", 24), _row("d1", 26), _row("d1", 28)]
        out = merge_appended(base, extras)
        assert [r["id"] for r in out] == ["d1#25", "d1#24", "d1#26", "d1#28"]

    def test_skips_already_present_chunk(self):
        base = [_row("d1", 25)]
        out = merge_appended(base, [_row("d1", 25), _row("d1", 26)])
        assert [r["id"] for r in out] == ["d1#25", "d1#26"]

    def test_budget_skips_oversized_and_keeps_going(self):
        base = [_row("d1", 25, "a" * 100)]
        extras = [_row("d1", 26, "b" * 5000), _row("d1", 27, "c" * 50)]
        out = merge_appended(base, extras, budget_chars=200)
        assert [r["id"] for r in out] == ["d1#25", "d1#27"]

    def test_base_never_dropped(self):
        base = [_row("d1", 25, "a" * 500)]
        out = merge_appended(base, [_row("d1", 26, "b" * 10)], budget_chars=100)
        assert [r["id"] for r in out] == ["d1#25"]

    def test_none_budget_is_unlimited(self):
        base = [_row("d1", 25, "a" * 500)]
        out = merge_appended(base, [_row("d1", 26, "b" * 10)], budget_chars=None)
        assert len(out) == 2
