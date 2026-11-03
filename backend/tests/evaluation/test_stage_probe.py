"""检索逐级探针的纯函数测试（P1-8）。

只测**判定逻辑**，不断言具体覆盖率数字——数字随文档内容变，
锁死会让测试变成「改一行文档就红」。守的是四件事：

1. 目录块识别不能把正常表格/正文误判成目录（误判 = 真实内容被踢出索引）；
2. 邻块扩展必须尊重「同标题」约束（跨标题扩展会把无关章节卷进上下文）；
3. 覆盖率是「命中比例」而不是「命中了没有」（概览型的失败形态是拼不全）；
4. 事实串的 chunk 命中数能算出来（覆盖率可信度全靠它）。

另外守一条**事故防线**：探针依赖 `run_eval` / `run_channel_ab` 的索引与重排实现，
这两个脚本改动时若签名变了，探针会静默跑出错误结论——所以这里断言它们仍可导入。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

from probe_retrieval_stages import (  # noqa: E402
    coverage,
    expand_ids,
    fact_document_frequency,
    is_toc_chunk,
)


def _chunk(doc: str, idx: int, heading: str, text: str) -> dict:
    return {"id": f"{doc}#{idx}", "title": doc, "heading_path": heading, "text": text}


class TestIsTocChunk:
    def test_detects_markdown_toc(self):
        toc = "部署指南 > 目录\n1. [环境要求](#1-环境要求)\n2. [部署](#2-部署)\n3. [监控](#3-监控)"
        assert is_toc_chunk(toc) is True

    def test_two_link_lines_is_not_toc(self):
        """两行不足以判目录：正文里的有序列表 + 链接很常见，误判会丢真实内容。"""
        text = "参考文档\n1. [架构](#架构)\n2. [部署](#部署)"
        assert is_toc_chunk(text) is False

    def test_normal_table_not_toc(self):
        table = "监控 > 指标\n| 指标 | 说明 |\n| rag_request_total | 请求总数 |\n| rag_active_requests | 活跃数 |"
        assert is_toc_chunk(table) is False

    def test_prose_with_numbered_list_not_toc(self):
        """没有 markdown 链接序号行就不算目录（避免把步骤说明当目录丢掉）。"""
        steps = "1. 先停服务\n2. 再备份\n3. 最后恢复\n4. 验证"
        assert is_toc_chunk(steps) is False


class TestExpandIds:
    def test_same_heading_only(self):
        corpus = {
            "a.md#0": _chunk("a.md", 0, "H1 > S1", "x"),
            "a.md#1": _chunk("a.md", 1, "H1 > S1", "y"),
            "a.md#2": _chunk("a.md", 2, "H1 > S2", "z"),
        }
        assert expand_ids("a.md#1", corpus, radius=1, same_heading_only=True) == ["a.md#0", "a.md#1"]

    def test_cross_heading_when_disabled(self):
        corpus = {
            "a.md#0": _chunk("a.md", 0, "H1 > S1", "x"),
            "a.md#1": _chunk("a.md", 1, "H1 > S1", "y"),
            "a.md#2": _chunk("a.md", 2, "H1 > S2", "z"),
        }
        assert expand_ids("a.md#1", corpus, radius=1, same_heading_only=False) == [
            "a.md#0",
            "a.md#1",
            "a.md#2",
        ]

    def test_boundary_does_not_escape_document(self):
        """越界不能串到别的文档：文件名不同，下标碰巧相接时最容易串。"""
        corpus = {
            "a.md#0": _chunk("a.md", 0, "H", "x"),
            "b.md#10": _chunk("b.md", 10, "H", "y"),
        }
        assert expand_ids("a.md#0", corpus, radius=2, same_heading_only=False) == ["a.md#0"]

    def test_unknown_id_returns_itself(self):
        assert expand_ids("missing#3", {}, radius=1, same_heading_only=True) == ["missing#3"]


class TestCoverage:
    def test_partial_coverage(self):
        frac, hits = coverage(["SIGTERM 与 SIGKILL"], ["SIGTERM", "SIGKILL", "drain"])
        assert frac == pytest.approx(2 / 3)
        assert hits == ["SIGTERM", "SIGKILL"]

    def test_empty_facts_is_zero_not_error(self):
        assert coverage(["whatever"], []) == (0.0, [])

    def test_multiple_texts_are_joined(self):
        frac, _ = coverage(["前半句 SIGTERM", "后半句 drain"], ["SIGTERM", "drain"])
        assert frac == 1.0


class TestFactDocumentFrequency:
    def test_counts_chunks_not_occurrences(self):
        corpus = [
            _chunk("a.md", 0, "H", "Milvus Milvus Milvus"),
            _chunk("a.md", 1, "H", "Milvus"),
            _chunk("a.md", 2, "H", "nothing"),
        ]
        assert fact_document_frequency(corpus, ["Milvus"]) == {"Milvus": 2}


def test_probe_dependencies_still_importable():
    """探针复用 run_eval / run_channel_ab 的索引与分数融合实现。

    这两个脚本重构时若改了导出名，探针会 ImportError 而不是静默给出错数字——
    这里显式断言一次，让 CI 在重构时就红，而不是等下一次跑探针才发现。
    """
    from probe_retrieval_stages import (  # noqa: F401
        build_index,
        build_reranker,
        rank_with_index,
        rrf_fuse,
    )

    assert callable(build_index) and callable(rank_with_index)
    assert callable(rrf_fuse) and callable(build_reranker)


class TestTableHeaderOf:
    """行级表格块识别：决定「整表回填」能不能触发，误判会把无关块拉进上下文。"""

    def test_row_chunk_returns_header(self):
        from probe_retrieval_stages import _table_header_of

        text = "监控 > 指标\n| 指标 | 类型 |\n| rag_request_total | Counter |"
        assert _table_header_of(text) == "| 指标 | 类型 |"

    def test_full_table_chunk_is_not_row_chunk(self):
        """三行以上的表格块已经是一整张表，不需要再回填。"""
        from probe_retrieval_stages import _table_header_of

        text = "监控 > 指标\n| 指标 | 类型 |\n| a | Counter |\n| b | Gauge |"
        assert _table_header_of(text) is None

    def test_prose_chunk_returns_none(self):
        from probe_retrieval_stages import _table_header_of

        assert _table_header_of("监控 > 概述\n这里是一段说明文字，没有表格。") is None

    def test_heading_path_alone_is_not_table(self):
        """只有 heading_path 没有正文时不能误判成表头。"""
        from probe_retrieval_stages import _table_header_of

        assert _table_header_of("监控 > 指标") is None


class TestTableRefill:
    def _corpus(self):
        return [
            # 同一张表的三行
            _chunk("deployment.md", 0, "监控 > 指标", "监控 > 指标\n| 指标 | 类型 |\n| rag_request_total | Counter |"),
            _chunk("deployment.md", 1, "监控 > 指标", "监控 > 指标\n| 指标 | 类型 |\n| rag_active_requests | Gauge |"),
            _chunk("deployment.md", 2, "监控 > 指标", "监控 > 指标\n| 指标 | 类型 |\n| rag_llm_calls_total | Counter |"),
            # 同文档的另一张表
            _chunk("deployment.md", 3, "监控 > 告警", "监控 > 告警\n| 告警 | 触发条件 |\n| ScrapeTargetDown | up == 0 |"),
            # 另一个文档的同名表头（不能跨文档回填）
            _chunk("other.md", 0, "监控 > 指标", "监控 > 指标\n| 指标 | 类型 |\n| other_metric | Counter |"),
        ]

    def test_refills_same_table_only(self):
        from probe_retrieval_stages import table_refill

        corpus = self._corpus()
        by_id = {c["id"]: c for c in corpus}

        out = table_refill(["deployment.md#0"], corpus, by_id)

        assert set(out) == {"deployment.md#0", "deployment.md#1", "deployment.md#2"}

    def test_does_not_pull_other_tables_or_documents(self):
        from probe_retrieval_stages import table_refill

        corpus = self._corpus()
        by_id = {c["id"]: c for c in corpus}

        out = table_refill(["deployment.md#0"], corpus, by_id)

        assert "deployment.md#3" not in out, "别的表不能混进来"
        assert "other.md#0" not in out, "别的文档不能混进来"

    def test_respects_char_cap(self):
        """字符上限兜底：大表分多批，不能把上下文撑爆。"""
        from probe_retrieval_stages import table_refill

        corpus = self._corpus()
        by_id = {c["id"]: c for c in corpus}

        out = table_refill(["deployment.md#0"], corpus, by_id, cap_chars=80)

        assert out == ["deployment.md#0"], "超出上限的兄弟行不该被拉进来"

    def test_non_table_chunk_is_untouched(self):
        from probe_retrieval_stages import table_refill

        corpus = self._corpus() + [_chunk("deployment.md", 4, "监控 > 概述", "监控 > 概述\n纯文字段落。")]
        by_id = {c["id"]: c for c in corpus}

        assert table_refill(["deployment.md#4"], corpus, by_id) == ["deployment.md#4"]
