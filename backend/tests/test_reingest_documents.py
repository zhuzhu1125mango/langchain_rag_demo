"""文档重灌脚本的「不会写坏」保证（2026-11-10）。

重灌是这个仓库里少数**直接删生产向量**的操作，而且失败方式很隐蔽：
删除成功、写入成功、校验自洽，但内容其实是拿错的那份文件。
所以这里只钉三处：

1. 对象键解析：`minio://bucket/key` 必须拆对，否则备份/覆写会落到错对象；
2. `--doc` 解析：多篇一起重灌时不能错位配错文件名与路径；
3. 块数腰斩闸：新块数明显少于现状时必须拒绝，除非显式放行。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.reingest_documents import (  # noqa: E402
    PreconditionError,
    check_shrink,
    minio_key,
    parse_doc_specs,
)


class TestMinioKey:
    def test_strips_bucket_from_uri(self):
        assert (
            minio_key("minio://documents/documents/abc/source.md")
            == "documents/abc/source.md"
        )

    def test_keeps_nested_path(self):
        assert minio_key("minio://kb/a/b/c/d.pdf") == "a/b/c/d.pdf"

    def test_rejects_local_path(self):
        """本地路径不能当对象键用：覆写会静默失败或写错对象。"""
        with pytest.raises(PreconditionError):
            minio_key("/app/data/docs/deployment.md")

    def test_rejects_missing_key(self):
        with pytest.raises(PreconditionError):
            minio_key("minio://documents/")

    def test_rejects_missing_bucket(self):
        with pytest.raises(PreconditionError):
            minio_key("minio://documents")


class TestParseDocSpecs:
    def test_parses_name_and_path(self, tmp_path):
        src = tmp_path / "deployment.md"
        src.write_text("# x\n", encoding="utf-8")
        assert parse_doc_specs([f"deployment.md={src}"]) == [("deployment.md", str(src))]

    def test_keeps_order_for_multiple_docs(self, tmp_path):
        a = tmp_path / "a.md"
        b = tmp_path / "b.md"
        a.write_text("a", encoding="utf-8")
        b.write_text("b", encoding="utf-8")
        parsed = parse_doc_specs([f"a.md={a}", f"b.md={b}"])
        assert [name for name, _ in parsed] == ["a.md", "b.md"]

    def test_rejects_missing_equals(self, tmp_path):
        with pytest.raises(PreconditionError):
            parse_doc_specs(["/tmp/x.md"])

    def test_rejects_missing_source_file(self, tmp_path):
        with pytest.raises(PreconditionError):
            parse_doc_specs([f"deployment.md={tmp_path / 'nope.md'}"])

    def test_rejects_empty_list(self):
        with pytest.raises(PreconditionError):
            parse_doc_specs([])


class TestShrinkGuard:
    def test_allows_normal_growth(self):
        check_shrink(296, 332, allow_shrink=False)

    def test_allows_modest_shrink(self):
        """两成内的减少是正常的编辑删改，不该拦。"""
        check_shrink(100, 85, allow_shrink=False)

    def test_rejects_zero_chunks(self):
        with pytest.raises(PreconditionError):
            check_shrink(296, 0, allow_shrink=False)

    def test_rejects_halved_chunks(self):
        with pytest.raises(PreconditionError):
            check_shrink(296, 120, allow_shrink=False)

    def test_allow_shrink_opens_the_gate(self):
        check_shrink(296, 120, allow_shrink=True)

    def test_first_ingest_without_baseline_passes(self):
        """老记录 chunks_count 为 0（历史脏数据）时不该卡住重灌。"""
        check_shrink(0, 332, allow_shrink=False)
