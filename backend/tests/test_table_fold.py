"""行级表格块折叠的边界测试。

这些用例钉的是「什么该折叠、什么不该折叠」——判据一旦放宽，会把整表块和
普通相邻段落也折掉，而后者是正当的连续证据（优雅停机的步骤就是同章节相邻几块）。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.table_chunks import (  # noqa: E402
    fold_same_table_rows,
    table_header_of,
)


def _chunk(text, doc="d1", cid=None):
    return {"document_id": doc, "id": cid or f"{doc}#{abs(hash(text)) % 10000}", "content": text}


class TestTableHeaderOf:
    def test_row_level_chunk_is_recognized(self):
        text = (
            "监控运维指南 > 4. 告警规则（configs/prometheus/alerts.yml）\n"
            "| 组 | 告警 | 表达式（摘要） | 级别 | 含义 |\n"
            "| scrape | ScrapeTargetDown | `up == 0` 持续 2m | critical | 任一抓取目标不可达 |"
        )
        assert table_header_of(text) == "| 组 | 告警 | 表达式（摘要） | 级别 | 含义 |"

    def test_whole_table_chunk_is_not_recognized(self):
        """整表块（表头 + 多行）没有可折叠的兄弟行，必须原样保留。"""
        text = (
            "## 指标\n"
            "| 指标 | 含义 |\n"
            "| a | 1 |\n"
            "| b | 2 |"
        )
        assert table_header_of(text) is None

    def test_plain_paragraph_is_not_recognized(self):
        assert table_header_of("优雅停机的顺序是：先摘流量、再等连接排空、最后退出。") is None

    def test_empty_and_none_are_safe(self):
        assert table_header_of("") is None
        assert table_header_of(None) is None

    def test_heading_plus_two_rows_is_not_a_row_level_chunk(self):
        """3 个非空行但含两行表格数据 → 不是「表头 + 一行」，不该折叠。"""
        text = "| 表头 | x |\n| 行一 | 1 |\n| 行二 | 2 |"
        assert table_header_of(text) is None


class TestFoldSameTableRows:
    HEADER = "| 组 | 告警 | 表达式（摘要） | 级别 | 含义 |"

    def _row(self, name, doc="monitoring.md"):
        return _chunk(f"监控 > 4. 告警规则\n{self.HEADER}\n| {name} | ... |", doc=doc)

    def test_same_table_keeps_only_first(self):
        items = [self._row("a"), self._row("b"), self._row("c")]
        out = fold_same_table_rows(items)
        assert len(out) == 1
        assert out[0]["content"].endswith("| a | ... |")

    def test_different_documents_are_not_merged(self):
        """同表头但不同文档是两张不同的表，不能合并。"""
        items = [self._row("a", doc="monitoring.md"), self._row("a", doc="other.md")]
        assert len(fold_same_table_rows(items)) == 2

    def test_non_table_chunks_are_untouched(self):
        para = _chunk("优雅停机怎么做：先摘流量，再排空连接。")
        items = [para, self._row("a"), self._row("b")]
        out = fold_same_table_rows(items)
        assert out[0] is para
        assert len(out) == 2

    def test_order_is_preserved(self):
        """折叠只做「同表只留第一个」，不重排——重排由 rerank 负责。"""
        para = _chunk("普通段落")
        items = [self._row("a"), para, self._row("b")]
        out = fold_same_table_rows(items)
        assert [x["content"][-8:] for x in out] == [items[0]["content"][-8:], para["content"][-8:]]

    def test_input_is_not_mutated(self):
        items = [self._row("a"), self._row("b")]
        snapshot = list(items)
        fold_same_table_rows(items)
        assert items == snapshot

    def test_missing_fields_are_tolerated(self):
        assert fold_same_table_rows([{}, {"content": None}]) == [{}, {"content": None}]

    def test_two_different_tables_both_survive(self):
        h1 = self._row("a")
        h2 = _chunk("监控 > 5. 面板\n| 面板 | 用途 |\n| Grafana | 看板 |")
        out = fold_same_table_rows([h1, h2])
        assert len(out) == 2


class TestConfigDefault:
    def test_disabled_by_default(self):
        """默认关闭：这次的收益约半道题，不足以直接改线上行为。"""
        from src.config import settings

        assert settings.processing.KB_TABLE_FOLD_ENABLED is False
