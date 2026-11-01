"""一致性巡检的判定逻辑测试（CODE-2 第二十六批）。

巡检工具本身最容易「跑通了但结论是假阴性」：判定条件写错、字段取错、把正常
状态报成异常，都会让人不再看报告。这里用纯函数把规则钉死。
"""

import uuid

import pytest

from src.services import consistency_audit as mod
from src.services.consistency_audit import (
    CacheEntryView,
    DbSnapshot,
    SectionResult,
    VectorRef,
    classify_cache,
    classify_missing_vectors,
    classify_objects,
    classify_vectors,
    decide_exit_code,
    to_object_key,
)


def _snapshot(**overrides) -> DbSnapshot:
    snap = DbSnapshot()
    snap.doc_status = overrides.get("doc_status", {})
    snap.doc_kb = overrides.get("doc_kb", {})
    snap.doc_chunks = overrides.get("doc_chunks", {})
    snap.wiki_ids = overrides.get("wiki_ids", set())
    snap.kb_ids = overrides.get("kb_ids", set())
    snap.doc_object_keys = overrides.get("doc_object_keys", {})
    snap.wiki_object_keys = overrides.get("wiki_object_keys", {})
    return snap


# ---------------------------------------------------------------------------
# 对象键解析
# ---------------------------------------------------------------------------
class TestObjectKey:
    def test_minio_path_stripped(self):
        from src.config import settings

        bucket = settings.minio.MINIO_BUCKET_NAME
        assert to_object_key(f"minio://{bucket}/documents/x/source.pdf") == "documents/x/source.pdf"

    def test_non_minio_paths_ignored(self):
        """本地文件、空值不能算「DB 引用的对象」，否则会误报 missing_object。"""
        assert to_object_key("") is None
        assert to_object_key("/tmp/a.pdf") is None
        assert to_object_key("minio://other-bucket/a.pdf") is None


# ---------------------------------------------------------------------------
# Milvus
# ---------------------------------------------------------------------------
class TestClassifyVectors:
    def test_memory_vectors_are_skipped(self):
        """Agent 记忆切片的 document_id 本就不在 DB，不能报成孤儿。"""
        refs = [
            VectorRef("uuid5-of-session", kb_id="memory", source_kind="memory", chunks=3),
            VectorRef("uuid5-of-session-2", kb_id="memory", source_kind="", chunks=1),
        ]
        assert classify_vectors(refs, _snapshot()) == []

    def test_healthy_document_produces_nothing(self):
        kb = str(uuid.uuid4())
        doc = str(uuid.uuid4())
        snap = _snapshot(
            doc_status={doc: "published"}, doc_kb={doc: kb}, kb_ids={kb}
        )
        assert classify_vectors([VectorRef(doc, kb, "raw", 10)], snap) == []

    def test_orphan_when_document_deleted(self):
        kb = str(uuid.uuid4())
        doc = str(uuid.uuid4())
        snap = _snapshot(kb_ids={kb})
        findings = classify_vectors([VectorRef(doc, kb, "raw", 10)], snap)

        kinds = {f.kind for f in findings}
        assert "orphan_vector_doc" in kinds
        orphan = next(f for f in findings if f.kind == "orphan_vector_doc")
        assert orphan.subject == doc and orphan.detail["chunks"] == 10

    def test_wiki_page_vector_is_not_orphan(self):
        page = str(uuid.uuid4())
        kb = str(uuid.uuid4())
        snap = _snapshot(wiki_ids={page}, kb_ids={kb})
        assert classify_vectors([VectorRef(page, kb, "wiki", 5)], snap) == []

    def test_unpublished_document_still_has_vectors(self):
        """下架/归档后向量必须删掉；还有残留说明检索仍能命中它。"""
        kb = str(uuid.uuid4())
        doc = str(uuid.uuid4())
        snap = _snapshot(
            doc_status={doc: "archived"}, doc_kb={doc: kb}, kb_ids={kb}
        )
        findings = classify_vectors([VectorRef(doc, kb, "raw", 4)], snap)

        assert [f.kind for f in findings] == ["vector_for_unpublished_doc"]
        assert findings[0].detail["status"] == "archived"

    def test_kb_mismatch_after_move(self):
        """文档移库后向量 kb_id 仍是旧的 → 会在旧库里被检索到。"""
        old_kb, new_kb = str(uuid.uuid4()), str(uuid.uuid4())
        doc = str(uuid.uuid4())
        snap = _snapshot(
            doc_status={doc: "published"}, doc_kb={doc: new_kb}, kb_ids={old_kb, new_kb}
        )
        findings = classify_vectors([VectorRef(doc, old_kb, "raw", 2)], snap)

        assert [f.kind for f in findings] == ["vector_kb_mismatch"]
        assert findings[0].detail["vector_kb"] == old_kb

    def test_blank_kb_id_is_reported(self):
        """历史切片 kb_id 为空：按知识库删永远删不到它。"""
        doc = str(uuid.uuid4())
        snap = _snapshot(doc_status={doc: "published"}, doc_kb={doc: ""})
        findings = classify_vectors([VectorRef(doc, "", "raw", 3)], snap)

        assert [f.kind for f in findings] == ["vector_kb_blank"]

    def test_kb_missing(self):
        doc = str(uuid.uuid4())
        gone = str(uuid.uuid4())
        snap = _snapshot(doc_status={doc: "published"}, doc_kb={doc: gone}, kb_ids=set())
        findings = classify_vectors([VectorRef(doc, gone, "raw", 3)], snap)

        assert {f.kind for f in findings} == {"vector_kb_missing"}

    def test_classify_missing_vectors_only_for_published_with_chunks(self):
        pub, draft, empty = (str(uuid.uuid4()) for _ in range(3))
        kb = str(uuid.uuid4())
        snap = _snapshot(
            doc_status={pub: "published", draft: "draft", empty: "published"},
            doc_kb={pub: kb, draft: kb, empty: kb},
            doc_chunks={pub: 12, draft: 12, empty: 0},
            kb_ids={kb},
        )

        findings = classify_missing_vectors({}, snap)

        assert [f.subject for f in findings] == [pub]
        assert findings[0].detail["chunks_count"] == 12

    def test_classify_missing_vectors_ignores_indexed_docs(self):
        doc = str(uuid.uuid4())
        snap = _snapshot(
            doc_status={doc: "published"}, doc_chunks={doc: 3}, doc_kb={doc: ""}
        )
        refs = {doc: VectorRef(doc, "", "raw", 3)}
        assert classify_missing_vectors(refs, snap) == []


# ---------------------------------------------------------------------------
# MinIO
# ---------------------------------------------------------------------------
class TestClassifyObjects:
    def test_both_directions(self):
        findings = classify_objects(
            object_keys={"documents/a/source.pdf", "documents/orphan/source.pdf"},
            expected_keys={"documents/a/source.pdf", "documents/lost/source.pdf"},
        )
        kinds = {f.kind: f.subject for f in findings}
        assert kinds == {
            "orphan_object": "documents/orphan/source.pdf",
            "missing_object": "documents/lost/source.pdf",
        }

    def test_clean_bucket(self):
        assert classify_objects({"wiki/kb/p.md"}, {"wiki/kb/p.md"}) == []


# ---------------------------------------------------------------------------
# Redis（语义缓存）
# ---------------------------------------------------------------------------
def _entry(source_docs, kb_ids, created_at=1_000.0, question="Q"):
    return CacheEntryView(
        key="rag:semcache:u1:abc",
        field="exact:deadbeef",
        question=question,
        created_at=created_at,
        kb_ids=list(kb_ids),
        source_docs=list(source_docs),
    )


class TestClassifyCache:
    def test_stale_when_document_deleted(self):
        gone = str(uuid.uuid4())
        kb = str(uuid.uuid4())
        snap = _snapshot(kb_ids={kb})
        findings = classify_cache([_entry([(gone, "raw")], [kb])], snap, ttl_seconds=0)

        assert [f.kind for f in findings] == ["stale_cache_entry"]
        assert gone in findings[0].detail["missing_document_ids"]

    def test_stale_when_document_unpublished(self):
        doc = str(uuid.uuid4())
        kb = str(uuid.uuid4())
        snap = _snapshot(doc_status={doc: "draft"}, kb_ids={kb})
        findings = classify_cache([_entry([(doc, "raw")], [kb])], snap, ttl_seconds=0)

        assert [f.kind for f in findings] == ["stale_cache_entry"]

    def test_wiki_source_checked_against_wiki_pages(self):
        page = str(uuid.uuid4())
        kb = str(uuid.uuid4())
        snap = _snapshot(wiki_ids={page}, kb_ids={kb})

        assert classify_cache([_entry([(page, "wiki")], [kb])], snap, ttl_seconds=0) == []

    def test_wiki_page_deleted_is_stale(self):
        page = str(uuid.uuid4())
        kb = str(uuid.uuid4())
        snap = _snapshot(kb_ids={kb})
        findings = classify_cache([_entry([(page, "wiki")], [kb])], snap, ttl_seconds=0)

        assert [f.kind for f in findings] == ["stale_cache_entry"]

    def test_all_scope_is_valid(self):
        """未选库的条目 kb_ids=["all"]，不能报成「知识库不存在」。"""
        doc = str(uuid.uuid4())
        snap = _snapshot(doc_status={doc: "published"})
        assert classify_cache([_entry([(doc, "raw")], ["all"])], snap, ttl_seconds=0) == []

    def test_deleted_kb_scope(self):
        doc = str(uuid.uuid4())
        gone_kb = str(uuid.uuid4())
        snap = _snapshot(doc_status={doc: "published"}, kb_ids=set())
        findings = classify_cache([_entry([(doc, "raw")], [gone_kb])], snap, ttl_seconds=0)

        assert [f.kind for f in findings] == ["stale_cache_kb"]

    def test_expired_entry(self):
        doc = str(uuid.uuid4())
        kb = str(uuid.uuid4())
        snap = _snapshot(doc_status={doc: "published"}, kb_ids={kb})
        findings = classify_cache(
            [_entry([(doc, "raw")], [kb], created_at=1.0)], snap, ttl_seconds=3600, now=10_000.0
        )

        assert [f.kind for f in findings] == ["expired_cache_entry"]

    def test_entry_without_document_id_is_ignored(self):
        """老条目没有 document_id：无从判断，不能报脏。"""
        kb = str(uuid.uuid4())
        snap = _snapshot(kb_ids={kb})
        assert classify_cache([_entry([], [kb])], snap, ttl_seconds=0) == []

    def test_entry_view_extracts_kb_sources(self):
        entry = {
            "question": "RAG 是什么",
            "created_at": 123.0,
            "kb_ids": ["kb-1"],
            "source_metadata": [
                {"source_type": "kb", "document_id": "d1", "source_kind": "raw"},
                {"source_type": "web", "title": "网页"},
                {"source_type": "kb", "document_id": "p1", "source_kind": "wiki"},
                {"source_type": "kb", "document_id": ""},
            ],
        }

        view = mod._entry_view("k", "f", entry)

        assert view.source_docs == [("d1", "raw"), ("p1", "wiki")]
        assert view.kb_ids == ["kb-1"]
        assert view.question == "RAG 是什么"


# ---------------------------------------------------------------------------
# 报告与退出码
# ---------------------------------------------------------------------------
class TestReport:
    def test_summary_counts_and_samples(self):
        section = SectionResult(name="milvus", scanned=10, total=10)
        section.findings = [
            mod.Finding("orphan_vector_doc", f"doc-{i}", {"chunks": i}) for i in range(3)
        ]

        summary = section.summary(sample=2)

        assert summary["issues"] == 3
        assert summary["kinds"]["orphan_vector_doc"]["count"] == 3
        assert len(summary["kinds"]["orphan_vector_doc"]["samples"]) == 2
        assert "元数据没了" in summary["kinds"]["orphan_vector_doc"]["label"]

    def test_exit_code_mapping(self):
        assert decide_exit_code({"sections": {}, "issue_count": 0}) == 0
        assert decide_exit_code(
            {"sections": {"milvus": {"error": None, "kinds": {}}}, "issue_count": 2}
        ) == 1
        assert decide_exit_code(
            {"sections": {"minio": {"error": "S3 down"}}, "issue_count": 0}
        ) == 2

    @pytest.mark.asyncio
    async def test_run_audit_aggregates_and_hides_internal_keys(self, monkeypatch):
        """编排层要聚合三侧结果，且不能把内部临时字段写进报告。"""
        doc = str(uuid.uuid4())
        kb = str(uuid.uuid4())
        snap = _snapshot(doc_status={doc: "published"}, doc_kb={doc: kb}, kb_ids={kb})

        async def _load():
            return snap

        async def _vectors(batch_size, max_offset):
            section = SectionResult(name="milvus", scanned=1, total=1)
            return section, {doc: VectorRef(doc, kb, "raw", 1)}

        async def _objects(max_objects):
            section = SectionResult(name="minio", scanned=2, total=2)
            section.extra["_keys"] = ["documents/a/source.pdf"]
            return section

        async def _cache(max_keys):
            section = SectionResult(name="redis", scanned=0)
            return section, []

        monkeypatch.setattr(mod, "load_db_snapshot", _load)
        monkeypatch.setattr(mod, "scan_vectors", _vectors)
        monkeypatch.setattr(mod, "scan_objects", _objects)
        monkeypatch.setattr(mod, "scan_cache", _cache)
        # 让该文档的向量成为孤儿（模拟元数据已删）
        snap.doc_status = {}

        report = await mod.run_audit()

        assert report["issue_count"] == 2  # 一个孤儿向量 + 一个无主对象
        assert "_keys" not in report["sections"]["minio"]["extra"]
        assert report["exit_code"] == 1


# ---------------------------------------------------------------------------
# CLI：退出码与报告输出（脚本是 nightly 的调用入口，不能只测逻辑层）
# ---------------------------------------------------------------------------
def _load_cli():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / "consistency_audit.py"
    spec = importlib.util.spec_from_file_location("consistency_audit_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _report(issue_count=0, exit_code=0, error=None):
    return {
        "generated_at": "2026-11-01T00:00:00+00:00",
        "duration_seconds": 1.0,
        "db": {"documents": 3, "published": 2, "knowledge_bases": 1,
               "wiki_pages": 0, "referenced_objects": 3},
        "sections": {
            "milvus": {
                "scanned": 10, "total": 10, "truncated": False, "error": error,
                "issues": issue_count,
                "kinds": (
                    {
                        "orphan_vector_doc": {
                            "label": mod.KIND_LABELS["orphan_vector_doc"],
                            "count": 1,
                            "samples": [{"subject": "doc-1", "chunks": 4}],
                        }
                    }
                    if issue_count
                    else {}
                ),
            }
        },
        "issue_count": issue_count,
        "exit_code": exit_code,
    }


class TestCli:
    def test_exit_code_propagates(self, monkeypatch, capsys):
        cli = _load_cli()

        async def _fake(**kwargs):
            return _report(issue_count=1, exit_code=1)

        monkeypatch.setattr(cli, "run_audit", _fake)

        assert cli.main([]) == 1
        out = capsys.readouterr().out
        assert "orphan_vector_doc" in out
        assert "退出码 1" in out

    def test_json_report_written(self, monkeypatch, tmp_path):
        import json

        cli = _load_cli()

        async def _fake(**kwargs):
            return _report()

        monkeypatch.setattr(cli, "run_audit", _fake)
        target = tmp_path / "audit.json"

        assert cli.main(["--json-out", str(target), "--quiet"]) == 0
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["issue_count"] == 0

    def test_collect_failure_does_not_print_all_clear(self, monkeypatch, capsys):
        """采集失败时打「未发现差异」= 假阴性，值班的人会被骗过去。"""
        cli = _load_cli()

        async def _fake(**kwargs):
            return _report(issue_count=0, exit_code=2, error="ConnectionError: minio down")

        monkeypatch.setattr(cli, "run_audit", _fake)
        code = cli.main([])

        out = capsys.readouterr().out
        assert code == 2
        assert "未发现差异" not in out
        assert "巡检未完整执行" in out
