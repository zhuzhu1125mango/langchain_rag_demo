"""Wiki 级联清理单元测试（Phase 2 + P5 级联重写，全 mock，无外部服务依赖）。

覆盖：源文档删除（剪源 / 孤儿页三处清理 / 索引页重建 / 失败不抛出）、
知识库删除（行 + MinIO 清理 / 空库早退）、
P5 级联重写（剩余来源重推导 / 无材料删页 / 单来源拉取失败降级 / 重写失败保留原内容）。
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

        from src.services.wiki_lock import get_kb_lock

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


# ----------------------------------------------------------------------
# P5 级联重写
# ----------------------------------------------------------------------
async def _drain_rewrites(timeout: float = 5.0) -> None:
    """等待级联重写后台任务全部结束（防止任务悬挂掩盖断言）。"""
    import asyncio

    from src.services import wiki_cascade

    tasks = [t for t in wiki_cascade._rewrite_tasks if not t.done()]
    if not tasks:
        return
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    assert not pending, "级联重写后台任务超时未完成"


@pytest.fixture
def rewrite_env(monkeypatch):
    """级联重写全套 mock：基础设施 + 编译器方法 + 独立会话 + 开关，返回记录器。"""
    from contextlib import asynccontextmanager

    from src.config import settings
    from src.services.wiki_compiler import WikiCompiler

    rec = {
        "chunks": {},          # doc_id -> [chunk]
        "fail_docs": set(),    # 拉取时抛错的 doc_id
        "regen_exc": None,     # regenerate_page 抛错
        "regen": [],           # (title, page_type, material)
        "uploads": {},         # content_path -> content
        "indexed": [],         # (kb_id, page_id, delete_old)
        "links": [],           # (kb_id, [page_id], [content])
        "rebuilds": 0,
        "vectors": [],
        "objects": [],
        "load_calls": [],      # (kb_id, doc_id)
        "select_queue": [],    # 重写会话的 select 结果队列
        "rewrite_db": None,    # 重写会话（enter 后可断言 delete/commit）
    }

    class FakeMilvus:
        async def delete_by_document_id(self, document_id):
            rec["vectors"].append(document_id)

    class FakeVectorStore:
        milvus_service = FakeMilvus()

    class FakeMinio:
        async def delete_file_async(self, path):
            rec["objects"].append(path)

        async def upload_text_async(self, key, content):
            rec["uploads"][key] = content
            return key

    async def fake_vector_get():
        return FakeVectorStore()

    async def fake_minio_get():
        return FakeMinio()

    monkeypatch.setattr("src.services.vector_store.VectorStoreManager.get_instance", fake_vector_get)
    monkeypatch.setattr("src.services.minio_service.MinioService.get_instance", fake_minio_get)

    async def fake_regen(self, title, page_type, material):
        if rec["regen_exc"] is not None:
            raise rec["regen_exc"]
        rec["regen"].append((title, page_type, material))
        return f"# {title}\n\n重写后的内容"

    async def fake_index(self, kb_id, row, page, delete_old=False):
        rec["indexed"].append((kb_id, row.id, delete_old))
        return 1

    async def fake_links(self, db, kb_id, rows, contents):
        rec["links"].append((kb_id, [r.id for r in rows], list(contents)))

    async def fake_rebuild(self, db, kb_id):
        rec["rebuilds"] += 1

    async def fake_load(kb_id, doc_id):
        rec["load_calls"].append((kb_id, doc_id))
        if doc_id in rec["fail_docs"]:
            raise ConnectionError(f"milvus down: {doc_id}")
        return list(rec["chunks"].get(doc_id, []))

    monkeypatch.setattr(WikiCompiler, "regenerate_page", fake_regen)
    monkeypatch.setattr(WikiCompiler, "_index_page", fake_index)
    monkeypatch.setattr(WikiCompiler, "_fill_links", fake_links)
    monkeypatch.setattr(WikiCompiler, "rebuild_index_page", fake_rebuild)
    monkeypatch.setattr("src.services.wiki_rebuild.load_raw_chunks", fake_load)
    monkeypatch.setattr(settings.wiki_compile, "WIKI_CASCADE_REWRITE", True)

    @asynccontextmanager
    async def maker():
        db = FakeDB(select_queue=rec["select_queue"])
        rec["rewrite_db"] = db
        yield db

    monkeypatch.setattr("src.database.async_session_maker", maker)
    return rec


class TestCascadeRewrite:
    async def test_multi_source_page_rewritten_from_remaining(self, rewrite_env):
        """剪源后仍有剩余来源 → 后台从剩余材料重推导，revision+1 并重入库。"""
        rec = rewrite_env
        page = make_page("page-1", ["doc-0", "doc-1"])
        db = FakeDB(select_queue=[[page]])
        rec["select_queue"] = [[page]]
        rec["chunks"] = {"doc-0": [SimpleNamespace(page_content="剩余材料内容")]}

        await on_document_deleted(db, "kb-1", "doc-1")
        assert page.source_doc_ids == ["doc-0"]  # 主流程仅剪源
        await _drain_rewrites()

        assert rec["load_calls"] == [("kb-1", "doc-0")]
        title, page_type, material = rec["regen"][0]
        assert (title, page_type) == ("页page-1", "entity")
        assert "剩余材料内容" in material
        assert rec["uploads"][page.content_path].startswith("# 页page-1")
        assert page.revision == 3  # 2 → 3
        assert rec["indexed"] == [("kb-1", "page-1", True)]
        assert rec["links"][0][:2] == ("kb-1", ["page-1"])
        assert rec["rebuilds"] == 1

    async def test_rewrite_disabled_no_background_task(self, patched_infra, monkeypatch):
        """开关关闭（默认）→ 行为与 Phase 2 完全一致，不调度后台任务。"""
        from src.config import settings

        monkeypatch.setattr(settings.wiki_compile, "WIKI_CASCADE_REWRITE", False)
        page = make_page("page-1", ["doc-0", "doc-1"])
        db = FakeDB(select_queue=[[page]])

        await on_document_deleted(db, "kb-1", "doc-1")
        assert wiki_cascade._rewrite_tasks == set()
        assert page.revision == 2  # 内容未动

    async def test_empty_material_deletes_page(self, rewrite_env):
        """剩余来源无有效材料 → 按孤儿页语义删除（向量 + MinIO + DB 行）。"""
        rec = rewrite_env
        page = make_page("page-1", ["doc-0", "doc-1"])
        db = FakeDB(select_queue=[[page]])
        rec["select_queue"] = [[page]]
        rec["chunks"] = {"doc-0": [SimpleNamespace(page_content="  ")]}

        await on_document_deleted(db, "kb-1", "doc-1")
        await _drain_rewrites()

        assert rec["regen"] == []
        assert rec["vectors"] == ["page-1"]
        assert rec["objects"] == [page.content_path]
        assert rec["rewrite_db"].deleted_rows == [page]
        assert rec["rewrite_db"].commit_count == 1
        assert rec["rebuilds"] == 1

    async def test_one_source_load_failure_still_rewrites(self, rewrite_env):
        """单个剩余来源拉取失败 → 跳过该来源，其余来源材料继续重写。"""
        rec = rewrite_env
        page = make_page("page-1", ["doc-a", "doc-b", "doc-1"])
        db = FakeDB(select_queue=[[page]])
        rec["select_queue"] = [[page]]
        rec["fail_docs"] = {"doc-a"}
        rec["chunks"] = {"doc-b": [SimpleNamespace(page_content="可用材料")]}

        await on_document_deleted(db, "kb-1", "doc-1")
        await _drain_rewrites()

        assert set(rec["load_calls"]) == {("kb-1", "doc-a"), ("kb-1", "doc-b")}
        assert "可用材料" in rec["regen"][0][2]
        assert rec["uploads"] and page.revision == 3

    async def test_rewrite_failure_keeps_current_content(self, rewrite_env):
        """重写 LLM 失败 → 保留当前内容（不写 MinIO、不动 revision、不重建索引）。"""
        rec = rewrite_env
        page = make_page("page-1", ["doc-0", "doc-1"])
        db = FakeDB(select_queue=[[page]])
        rec["select_queue"] = [[page]]
        rec["chunks"] = {"doc-0": [SimpleNamespace(page_content="材料")]}
        rec["regen_exc"] = RuntimeError("LLM down")

        await on_document_deleted(db, "kb-1", "doc-1")
        await _drain_rewrites()

        assert rec["uploads"] == {}
        assert page.revision == 2
        assert rec["indexed"] == []
        assert rec["links"] == []
        assert rec["rewrite_db"].commit_count == 0
        assert rec["rebuilds"] == 0
