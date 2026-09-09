"""KB 单删性能优化单元测试（#19）。

验证单删知识库复用批量删除逻辑：
- 向量按 kb_id 一次删除（不再逐文档删除）
- MinIO 使用异步并发清理（不再同步阻塞）
- 数据库记录用批量 SQL delete（不再逐 ORM delete）
"""

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import Select, Delete

from src.api.knowledge_base import delete_knowledge_base
from src.models import KnowledgeBase, Document
from src.models.wiki_page import WikiPage

USER_ID = "user-1"


class FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value


class FakeVectorStore:
    def __init__(self, error=None):
        self.kb_ids_calls = []
        self.by_document_calls = []
        self._error = error

    async def delete_by_kb_ids(self, kb_ids):
        if self._error:
            raise self._error
        self.kb_ids_calls.append(kb_ids)

    async def delete_by_document_id(self, document_id):
        self.by_document_calls.append(document_id)


class FakeMinio:
    def __init__(self, error=None):
        self.async_calls = []
        self.sync_calls = []
        self._error = error

    async def delete_file_async(self, file_path):
        if self._error:
            raise self._error
        self.async_calls.append(file_path)

    def delete_file(self, file_path):
        self.sync_calls.append(file_path)


class FakeDB:
    """记录 execute 语句类型序列的假 AsyncSession。"""

    def __init__(self, kb, docs):
        self._kb = kb
        self._docs = docs
        self.stmt_types = []
        self.commit_calls = 0
        self.orm_delete_calls = []

    async def execute(self, stmt, *args, **kwargs):
        if isinstance(stmt, Select):
            entity = stmt.column_descriptions[0]["entity"]
            if entity is KnowledgeBase:
                self.stmt_types.append("select_kb")
                return FakeResult(self._kb)
            if entity is WikiPage:
                # P2：Wiki 级联清理查询（本测试场景无 Wiki 页 → 级联早退）
                self.stmt_types.append("select_wiki_pages")
                return FakeResult([])
            self.stmt_types.append("select_docs")
            return FakeResult(self._docs)
        if isinstance(stmt, Delete):
            table = stmt.table.name
            self.stmt_types.append(f"delete_{table}")
            return FakeResult(None)
        self.stmt_types.append("other")
        return FakeResult(None)

    async def delete(self, obj):
        self.orm_delete_calls.append(obj)

    async def commit(self):
        self.commit_calls += 1


@pytest.fixture
def kb():
    return SimpleNamespace(id=uuid.uuid4(), owner_id=USER_ID, is_default=False)


@pytest.fixture
def current_user():
    return SimpleNamespace(user_id=USER_ID, is_authenticated=True)


def make_docs():
    return [
        SimpleNamespace(id=uuid.uuid4(), file_path="minio://bucket/a.pdf"),
        SimpleNamespace(id=uuid.uuid4(), file_path="minio://bucket/b.pdf"),
        SimpleNamespace(id=uuid.uuid4(), file_path="local://legacy.txt"),
        SimpleNamespace(id=uuid.uuid4(), file_path=None),
    ]


async def run_delete(kb, current_user, vector_store, minio_service):
    db = FakeDB(kb, make_docs())
    result = await delete_knowledge_base(
        kb_id=str(kb.id), db=db, current_user=current_user
    )
    return db, result


@pytest.fixture
def cleanup_fakes(monkeypatch):
    """注入假的向量/MinIO/缓存依赖，返回 fake 实例容器供断言使用。"""
    holder = {"vector": None, "minio": None}

    vector_store = FakeVectorStore()
    minio_service = FakeMinio()
    holder["vector"] = vector_store
    holder["minio"] = minio_service

    async def fake_vector_get_instance():
        return vector_store

    async def fake_minio_get_instance():
        return minio_service

    async def fake_invalidate(user_id):
        pass

    monkeypatch.setattr(
        "src.api.knowledge_base.VectorStoreManager.get_instance",
        fake_vector_get_instance,
    )
    monkeypatch.setattr(
        "src.services.minio_service.MinioService.get_instance",
        fake_minio_get_instance,
    )
    monkeypatch.setattr(
        "src.api.knowledge_base.invalidate_kb_list_cache", fake_invalidate
    )
    return holder


async def run_delete(kb, current_user):
    db = FakeDB(kb, make_docs())
    result = await delete_knowledge_base(
        kb_id=str(kb.id), db=db, current_user=current_user
    )
    return db, result


class TestKbSingleDelete:

    @pytest.mark.asyncio
    async def test_uses_batch_vector_cleanup(self, kb, current_user, cleanup_fakes):
        """向量按 kb_id 一次删除，不再逐文档删除。"""
        db, result = await run_delete(kb, current_user)

        assert cleanup_fakes["vector"].kb_ids_calls == [[str(kb.id)]]
        assert cleanup_fakes["vector"].by_document_calls == []
        assert result == {"message": "知识库已删除"}

    @pytest.mark.asyncio
    async def test_uses_async_minio_cleanup(self, kb, current_user, cleanup_fakes):
        """MinIO 使用 delete_file_async 并发清理，仅处理 minio:// 路径。"""
        await run_delete(kb, current_user)

        fake_minio = cleanup_fakes["minio"]
        assert sorted(fake_minio.async_calls) == ["minio://bucket/a.pdf", "minio://bucket/b.pdf"]
        assert fake_minio.sync_calls == []

    @pytest.mark.asyncio
    async def test_db_uses_batch_sql_delete(self, kb, current_user, cleanup_fakes):
        """数据库记录使用批量 SQL delete（先 Document 后 KB），无逐 ORM delete。

        末尾的 select_wiki_pages 为 P2 级联清理查询（无 Wiki 页 → 早退，不影响主流程）。
        """
        db, _ = await run_delete(kb, current_user)

        assert db.stmt_types == [
            "select_kb",
            "select_docs",
            "delete_documents",
            "delete_knowledge_bases",
            "select_wiki_pages",
        ]
        assert db.orm_delete_calls == []
        assert db.commit_calls == 1

    @pytest.mark.asyncio
    async def test_vector_failure_not_blocking(self, kb, current_user, cleanup_fakes, monkeypatch):
        """向量清理失败不阻塞数据库记录删除。"""
        broken = FakeVectorStore(error=RuntimeError("milvus down"))
        monkeypatch.setattr(
            "src.api.knowledge_base.VectorStoreManager.get_instance",
            _fake_get_instance(broken),
        )
        db, result = await run_delete(kb, current_user)

        assert result == {"message": "知识库已删除"}
        assert db.commit_calls == 1

    @pytest.mark.asyncio
    async def test_minio_failure_not_blocking(self, kb, current_user, cleanup_fakes, monkeypatch):
        """MinIO 清理失败不阻塞数据库记录删除。"""
        broken = FakeMinio(error=RuntimeError("minio down"))
        monkeypatch.setattr(
            "src.services.minio_service.MinioService.get_instance",
            _fake_get_instance(broken),
        )
        db, result = await run_delete(kb, current_user)

        assert result == {"message": "知识库已删除"}
        assert db.commit_calls == 1


def _fake_get_instance(instance):
    async def fake_get_instance():
        return instance
    return fake_get_instance
