"""Wiki 级联清理单元测试（Phase 2，全 mock，无外部服务依赖）。

覆盖：源文档删除（剪源 / 孤儿页三处清理 / 索引页重建 / 失败不抛出）、
知识库删除（行 + MinIO 清理 / 空库早退）。
"""

from types import SimpleNamespace
from typing import List

import pytest

from src.services import wiki_cascade
from src.services.wiki_cascade import on_document_deleted, on_kb_deleted


class FakeScalars:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items

    def first(self):
        return self._items[0] if self._items else None


class FakeResult:
    def __init__(self, items):
        self._scalars = FakeScalars(items)

    def scalars(self):
        return self._scalars


class FakeDB:
    """按调用次序弹出 select 结果的假会话（记录 delete/commit）。"""

    def __init__(self, select_queue):
        self.select_queue = list(select_queue)
        self.deleted_rows: List[object] = []
        self.executed_deletes: List[object] = []
        self.commit_count = 0

    async def execute(self, stmt):
        return FakeResult(self.select_queue.pop(0) if self.select_queue else [])

    async def delete(self, obj):
        self.deleted_rows.append(obj)

    async def commit(self):
        self.commit_count += 1


def make_page(page_id: str, doc_ids: List[str], kb_id: str = "kb-1") -> SimpleNamespace:
    return SimpleNamespace(
        id=page_id,
        kb_id=kb_id,
        page_type="entity",
        title=f"页{page_id}",
        content_path=f"wiki/{kb_id}/{page_id}.md",
        source_doc_ids=list(doc_ids),
        revision=2,
        status="active",
        owner_id=None,
    )


@pytest.fixture
def patched_infra(monkeypatch):
    """mock MinIO / 向量库 / WikiCompiler.rebuild_index_page。"""
    deleted = {"vectors": [], "objects": [], "index_rebuilds": 0}

    class FakeMilvus:
        async def delete_by_document_id(self, document_id):
            deleted["vectors"].append(document_id)

    class FakeVectorStore:
        milvus_service = FakeMilvus()

    class FakeMinio:
        async def delete_file_async(self, path):
            deleted["objects"].append(path)

    async def fake_vector_get():
        return FakeVectorStore()

    async def fake_minio_get():
        return FakeMinio()

    monkeypatch.setattr("src.services.vector_store.VectorStoreManager.get_instance", fake_vector_get)
    monkeypatch.setattr("src.services.minio_service.MinioService.get_instance", fake_minio_get)

    async def fake_rebuild_index(self, db, kb_id):
        deleted["index_rebuilds"] += 1

    from src.services.wiki_compiler import WikiCompiler

    monkeypatch.setattr(WikiCompiler, "rebuild_index_page", fake_rebuild_index)
    return deleted


class TestOnDocumentDeleted:
    async def test_no_affected_pages_returns_early(self, patched_infra):
        db = FakeDB(select_queue=[[]])
        await on_document_deleted(db, "kb-1", "doc-1")
        assert db.commit_count == 0
        assert patched_infra["vectors"] == []

    async def test_multi_source_page_pruned_not_deleted(self, patched_infra):
        page = make_page("page-1", ["doc-0", "doc-1"])
        db = FakeDB(select_queue=[[page]])
        await on_document_deleted(db, "kb-1", "doc-1")
        assert page.source_doc_ids == ["doc-0"]
        assert db.deleted_rows == []           # 页面保留
        assert patched_infra["vectors"] == []  # 不清向量
        assert patched_infra["objects"] == []  # 不删 MinIO 正文
        assert patched_infra["index_rebuilds"] == 0  # 无页面删除不重建索引
        assert db.commit_count == 1

    async def test_orphan_page_deleted_everywhere(self, patched_infra):
        page = make_page("page-1", ["doc-1"])
        db = FakeDB(select_queue=[[page]])
        await on_document_deleted(db, "kb-1", "doc-1")
        # 孤儿页：向量 + MinIO + DB 行三处清理
        assert patched_infra["vectors"] == ["page-1"]
        assert patched_infra["objects"] == ["wiki/kb-1/page-1.md"]
        assert db.deleted_rows == [page]
        assert db.commit_count == 1
        # 有页面删除 → 索引页重建
        assert patched_infra["index_rebuilds"] == 1

    async def test_error_swallowed_does_not_raise(self, patched_infra, monkeypatch):
        class BoomDB:
            async def execute(self, stmt):
                raise RuntimeError("db down")

        await on_document_deleted(BoomDB(), "kb-1", "doc-1")  # 不应抛出


class TestOnKbDeleted:
    async def test_no_pages_returns_early(self, patched_infra):
        db = FakeDB(select_queue=[[]])
        await on_kb_deleted(db, "kb-1")
        assert db.commit_count == 0
        assert patched_infra["vectors"] == []

    async def test_pages_deleted_with_artifacts(self, patched_infra):
        p1 = make_page("page-1", ["doc-1"])
        p2 = make_page("page-2", [], kb_id="kb-1")
        db = FakeDB(select_queue=[[p1, p2]])
        await on_kb_deleted(db, "kb-1")
        assert patched_infra["vectors"] == ["page-1", "page-2"]
        assert patched_infra["objects"] == [
            "wiki/kb-1/page-1.md",
            "wiki/kb-1/page-2.md",
        ]
        assert db.commit_count == 1

    async def test_error_swallowed_does_not_raise(self, patched_infra):
        class BoomDB:
            async def execute(self, stmt):
                raise RuntimeError("db down")

        await on_kb_deleted(BoomDB(), "kb-1")  # 不应抛出


class TestKbLockShared:
    async def test_cascade_shares_compiler_lock(self, patched_infra):
        """级联与编译共用同一把 KB 锁（互斥验证）。"""
        import asyncio

        from src.services.wiki_compiler import get_kb_lock

        page = make_page("page-1", ["doc-1"])
        db = FakeDB(select_queue=[[page]])

        lock = get_kb_lock("kb-cascade-shared")
        async with lock:
            task = asyncio.create_task(on_document_deleted(db, "kb-cascade-shared", "doc-1"))
            await asyncio.sleep(0.05)
            # 锁被编译侧持有 → 级联尚未执行到 select
            assert db.select_queue == [[page]]
        await task
        assert patched_infra["vectors"] == ["page-1"]
