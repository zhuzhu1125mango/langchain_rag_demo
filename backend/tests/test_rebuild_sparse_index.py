"""稀疏索引重建脚本的「不会误伤」保证（2026-11-09）。

脚本会原地改写 532 条切片的 sparse 向量，写错没人会立刻发现——症状是
「检索结果差一点」，不是报错。所以这里只钉两件事：

1. **拒绝执行**的边界：没读全、语料太小、没有 sparse 字段、主键重复，都必须
   在写入前拦下来；
2. **统计口径**：新旧词表覆盖率的分母/分子不能算错，否则「修好了」是自我欺骗。
"""

import json
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.rebuild_sparse_index import (  # noqa: E402
    PreconditionError,
    backup_vocab,
    check_preconditions,
    is_suspect,
    pick_self_check_rows,
    query_coverage,
    read_vocab_meta,
    sparse_stats,
)


def make_rows(count: int, dim: int = 8, content: str = "切片正文" * 20):
    return [
        {"id": f"id-{i}", "content": content, "embedding": [0.1] * dim}
        for i in range(count)
    ]


class TestSuspectHeuristic:
    def test_five_doc_vocab_on_532_rows_is_suspect(self):
        """事故现场：5 条演练文本的词表覆盖 532 条真实切片。"""
        assert is_suspect(5, 532) is True

    def test_full_fit_is_not_suspect(self):
        assert is_suspect(532, 532) is False

    def test_unknown_corpus_size_never_alarms(self):
        """老词表没有 corpus_size 时不能瞎报警，否则告警会被当噪声忽略。"""
        assert is_suspect(0, 532) is False


class TestSparseStats:
    def test_counts_empty_and_nonempty(self):
        stats = sparse_stats([{1: 0.5, 3: 1.0}, {}, {2: 0.7}, {}])
        assert stats == {
            "rows": 4,
            "nonempty": 2,
            "empty": 2,
            "ratio": 0.5,
            "avg_nnz": 0.75,
        }

    def test_empty_input_does_not_divide_by_zero(self):
        assert sparse_stats([])["ratio"] == 0.0


class TestPreconditions:
    def test_accepts_healthy_input(self):
        check_preconditions(make_rows(30), ("id", "content", "embedding", "sparse_embedding"), 500)

    def test_rejects_collection_without_sparse_field(self):
        with pytest.raises(PreconditionError, match="sparse_embedding"):
            check_preconditions(make_rows(30), ("id", "content", "embedding"), 500)

    def test_rejects_tiny_corpus(self):
        """在小语料上重拟合正是这次事故的成因，脚本不能自己再生产一份。"""
        with pytest.raises(PreconditionError, match="切片"):
            check_preconditions(make_rows(3), ("id", "content", "embedding", "sparse_embedding"), 500)

    def test_rejects_tiny_vocab(self):
        with pytest.raises(PreconditionError, match="个词"):
            check_preconditions(make_rows(30), ("id", "content", "embedding", "sparse_embedding"), 10)

    def test_rejects_duplicate_primary_keys(self):
        rows = make_rows(30)
        rows[1]["id"] = rows[0]["id"]
        with pytest.raises(PreconditionError, match="重复主键"):
            check_preconditions(rows, ("id", "content", "embedding", "sparse_embedding"), 500)

    def test_rejects_mixed_dense_dimensions(self):
        rows = make_rows(30)
        rows[0]["embedding"] = [0.1] * 4
        with pytest.raises(PreconditionError, match="维度"):
            check_preconditions(rows, ("id", "content", "embedding", "sparse_embedding"), 500)


class TestQueryCoverage:
    """「稀疏通道参不参与」看的是查询向量，不是存量向量。"""

    class _EF:
        def __init__(self, mapping):
            self._mapping = mapping

        def encode_queries(self, texts):
            return [np.array(self._mapping[text], dtype=float) for text in texts]

    def test_counts_only_positive_dimensions(self):
        ef = self._EF({"命中两个词": [0.5, -0.3, 0.0, 1.2], "全部 OOV": [0.0, 0.0, 0.0, 0.0]})
        coverage = query_coverage(ef, ["命中两个词", "全部 OOV"])
        assert [item["nnz"] for item in coverage] == [2, 0]

    def test_keeps_query_text_for_reporting(self):
        ef = self._EF({"备份": [1.0]})
        assert query_coverage(ef, ["备份"])[0]["query"] == "备份"


class TestSelfCheckSampling:
    def test_skips_short_chunks(self):
        """太短的切片自检索命中不了自己，不代表索引坏了；不能拿它做判据。"""
        rows = [{"id": "short", "content": "短"}, {"id": "long", "content": "正文" * 100}]
        assert [row["id"] for row in pick_self_check_rows(rows, 3)] == ["long"]

    def test_zero_sample_disables_check(self):
        assert pick_self_check_rows(make_rows(5), 0) == []


class TestVocabFileHandling:
    def test_read_meta_from_saved_payload(self, tmp_path):
        path = tmp_path / "bm25_documents.json"
        path.write_text(
            json.dumps({"corpus_size": 5, "avgdl": 138.8, "idf_word": ["a", "b", "c"]}),
            encoding="utf-8",
        )
        meta = read_vocab_meta(str(path))
        assert meta["corpus_size"] == 5
        assert meta["vocab_size"] == 3
        assert meta["avgdl"] == 138.8

    def test_missing_file_returns_none(self, tmp_path):
        assert read_vocab_meta(str(tmp_path / "nope.json")) is None

    def test_backup_keeps_original_content(self, tmp_path):
        path = tmp_path / "bm25_documents.json"
        path.write_text('{"corpus_size": 5}', encoding="utf-8")
        backup = backup_vocab(str(path))
        assert backup and os.path.exists(backup)
        assert open(backup, encoding="utf-8").read() == '{"corpus_size": 5}'
        # 备份不能把原文件挪走：写入新词表前，服务可能仍在读它
        assert os.path.exists(path)

    def test_backup_of_missing_file_is_none(self, tmp_path):
        assert backup_vocab(str(tmp_path / "nope.json")) is None
