"""一致性链路：元数据删了、向量还在（CODE-2 第二十四批）。

本批针对「反向静默」：删除/失败路径只改了 DB 或只改了状态，向量库没人管，
于是内容在检索里继续存在，日志里却什么都没有。覆盖：

1. 会话删除 / 批量删除 —— Agent 记忆切片（kb_id="memory"）从来没被清理；
2. 账号注销 —— 同上；文档/知识库向量清理失败只记 warning，属永久残留；
3. 文档处理失败 —— 向量已写入、文档标 failed，切片仍可被检索命中；
4. 文档删除顺序 —— 旧实现先删文件再删向量，中途失败即「文件没了、内容还能搜到」；
5. 知识库删除 —— 按 kb_id 删向量「成功但 0 影响」（历史 kb_id 为空）需回退逐文档删。
"""

import logging
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import Delete, Select

from src.services import agent_memory


# ---------------------------------------------------------------------------
# 通用假对象
# ---------------------------------------------------------------------------
class _Result:
    def __init__(self, rows=None, rowcount=0, one=None):
        self._rows = rows or []
        self._one = one
        self.rowcount = rowcount

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._one

    def scalar_one_or_none(self):
        return self._one


class _SeqDB:
    """按调用顺序返回预设结果，并记录执行过的语句。"""

    def __init__(self, results=(), default_one=None):
        self._results = list(results)
        self._default = _Result(one=default_one)
        self.statements = []
        self.commits = 0
        self.orm_deleted = []

    async def execute(self, stmt, *args, **kwargs):
        self.statements.append(stmt)
        return self._results.pop(0) if self._results else self._default

    async def delete(self, obj):
        self.orm_deleted.append(obj)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        return None


class _FakeVectorStore:
    def __init__(self, error=None, residual=None):
        self.batches = []
        self.single_deletes = []
        self.added = []
        self._error = error
        self._residual = list(residual or [])
        if residual is not None:
            self.milvus_service = SimpleNamespace(count_by_kb=self._count_by_kb)

    async def _count_by_kb(self, kb_id):
        return self._residual.pop(0) if self._residual else 0

    async def add_documents(self, chunks, kb_id):
        if self._error:
            raise self._error
        self.added.append((list(chunks), kb_id))

    async def delete_by_document_ids(self, doc_ids):
        if self._error:
            raise self._error
        self.batches.append(list(doc_ids))

    async def delete_by_document_id(self, doc_id):
        if self._error:
            raise self._error
        self.single_deletes.append(doc_id)

    async def delete_by_kb_ids(self, kb_ids):
        if self._error:
            raise self._error
        self.batches.append(list(kb_ids))


@pytest.fixture
def fake_vector(monkeypatch):
    """把 VectorStoreManager 换成可控假例；返回容器便于按需替换实例。"""
    holder = {"store": _FakeVectorStore()}

    async def _get_instance():
        return holder["store"]

    monkeypatch.setattr(
        "src.services.vector_store.VectorStoreManager.get_instance", _get_instance
    )
    return holder


# ---------------------------------------------------------------------------
# 1. Agent 记忆向量的生命周期
# ---------------------------------------------------------------------------
class TestAgentMemoryLifecycle:
    def test_memory_doc_id_is_stable_and_valid_uuid(self):
        """记忆 doc_id 必须是合法 UUID，否则 Milvus 的 _safe_id 白名单会拒绝清理。"""
        sid = str(uuid.uuid4())
        first = agent_memory.memory_doc_id(sid)
        assert first == agent_memory.memory_doc_id(sid)
        assert str(uuid.UUID(first)) == first
        # 写入侧（pipeline.state）与清理侧必须共用同一份规则
        from src.services.pipeline.state import _memory_doc_id

        assert _memory_doc_id(sid) == first

    @pytest.mark.asyncio
    async def test_delete_session_memories_batches(self, monkeypatch):
        """大量会话合并成有限批次的 delete 表达式，避免逐条 flush 触发限流。"""
        store = _FakeVectorStore()

        async def _get_instance():
            return store

        monkeypatch.setattr(
            "src.services.vector_store.VectorStoreManager.get_instance", _get_instance
        )
        sids = [str(uuid.uuid4()) for _ in range(250)]

        count = await agent_memory.delete_session_memories(
            sids, action="test.memory.cleanup"
        )

        assert count == 250
        assert [len(b) for b in store.batches] == [100, 100, 50]
        assert [d for b in store.batches for d in b] == [
            agent_memory.memory_doc_id(s) for s in sids
        ]

    @pytest.mark.asyncio
    async def test_delete_session_memories_failure_is_reportable(
        self, monkeypatch, caplog
    ):
        """残留对话摘要是隐私数据，失败必须是 error 级并带可检索的 action。"""
        store = _FakeVectorStore(error=RuntimeError("milvus down"))

        async def _get_instance():
            return store

        monkeypatch.setattr(
            "src.services.vector_store.VectorStoreManager.get_instance", _get_instance
        )
        sid = str(uuid.uuid4())

        with caplog.at_level(logging.ERROR, logger="rag_system"):
            count = await agent_memory.delete_session_memories(
                [sid], action="session.delete.memory_vectors", user_id="u-1"
            )

        assert count == 0
        hits = [
            r for r in caplog.records if "action=session.delete.memory_vectors" in r.getMessage()
        ]
        assert hits, "记忆清理失败不能静默"
        assert hits[0].levelno == logging.ERROR
        assert "severity=reportable" in hits[0].getMessage()

    @pytest.mark.asyncio
    async def test_delete_session_route_cleans_memory(self, monkeypatch):
        """删除会话必须连带清掉该会话的记忆向量（原先只删 DB 行）。"""
        from src.api.session import delete_session

        sid = str(uuid.uuid4())
        session = SimpleNamespace(id=uuid.UUID(sid), user_id="u-1", title="t")
        db = _SeqDB([_Result(one=session)])
        cleaned = []

        async def _record(session_ids, *, action, **ctx):
            cleaned.append((list(session_ids), action))
            return len(list(session_ids))

        monkeypatch.setattr("src.services.agent_memory.delete_session_memories", _record)

        result = await delete_session(
            sid, db=db, current_user=SimpleNamespace(user_id="u-1", is_authenticated=True)
        )

        assert result == {"message": "删除成功"}
        assert db.orm_deleted == [session]
        assert cleaned == [([sid], "session.delete.memory_vectors")]

    @pytest.mark.asyncio
    async def test_batch_delete_only_cleans_owned_sessions(self, monkeypatch):
        """批量删除只能按「确认属于当前用户」的 id 清记忆，否则会误删他人数据。"""
        from src.api.session import batch_delete_sessions

        mine = uuid.uuid4()
        foreign = uuid.uuid4()
        db = _SeqDB(
            [
                _Result(rows=[mine]),          # 归属校验：只查到当前用户的会话
                _Result(rowcount=0),           # 删反馈
                _Result(rowcount=1),           # 删会话
            ]
        )
        cleaned = []

        async def _record(session_ids, *, action, **ctx):
            cleaned.append(list(session_ids))
            return len(list(session_ids))

        monkeypatch.setattr("src.services.agent_memory.delete_session_memories", _record)

        result = await batch_delete_sessions(
            SimpleNamespace(ids=[str(mine), str(foreign)]),
            db=db,
            current_user=SimpleNamespace(user_id="u-1", is_authenticated=True),
        )

        assert result == {"deleted_count": 1}
        assert cleaned == [[str(mine)]], "不得把他人会话的记忆一并删除"


# ---------------------------------------------------------------------------
# 2. 文档处理失败：向量已写入 → 必须回滚
# ---------------------------------------------------------------------------
class TestDocumentFailureRollback:
    @pytest.mark.asyncio
    async def test_failure_after_vector_write_rolls_back(self, monkeypatch):
        """文档标 failed 后，已写入的切片仍可被检索命中——必须回滚。"""
        from src.services import document_tasks as mod

        doc_id = str(uuid.uuid4())
        doc = SimpleNamespace(
            id=uuid.UUID(doc_id),
            filename="a.pdf",
            status="draft",
            processing_status="processing",
            processing_message="",
            processing_progress=0,
            chunks_count=0,
            owner_id="u-1",
        )
        db = _SeqDB([_Result(one=doc)], default_one=doc)
        store = _FakeVectorStore()

        @asynccontextmanager
        async def _maker():
            yield db

        async def _get_instance():
            return store

        async def _noop(*args, **kwargs):
            return None

        class _Analyzer:
            @staticmethod
            def analyze_document_content(content):
                raise RuntimeError("analyzer boom")

            @staticmethod
            def evaluate_quality(content):
                return {}

        monkeypatch.setattr(mod, "async_session_maker", _maker)
        monkeypatch.setattr(mod, "process_document", lambda *a, **k: [
            SimpleNamespace(page_content="hello", metadata={})
        ])
        monkeypatch.setattr(mod, "DocumentAnalyzer", _Analyzer)
        monkeypatch.setattr(mod, "VectorStoreManager", SimpleNamespace(get_instance=_get_instance))
        monkeypatch.setattr(mod, "update_upload_progress", lambda *a, **k: None)
        monkeypatch.setattr(mod, "notify_task_progress", _noop)
        monkeypatch.setattr(mod, "notify_task_failed", _noop)

        await mod.process_document_async(doc_id, "raw/a.pdf", "kb-1", "task-1", None, None)

        assert doc.processing_status == "failed"
        assert store.single_deletes == [doc_id], "失败文档的残留向量没有被回滚"

    @pytest.mark.asyncio
    async def test_rollback_failure_is_reportable(self, monkeypatch, caplog):
        """回滚失败会让切片永久可检索，必须 error 级留痕而不是 warning。"""
        from src.services import document_tasks as mod

        store = _FakeVectorStore(error=RuntimeError("milvus down"))

        async def _get_instance():
            return store

        monkeypatch.setattr(
            mod, "VectorStoreManager", SimpleNamespace(get_instance=_get_instance)
        )
        doc_id = str(uuid.uuid4())

        with caplog.at_level(logging.ERROR, logger="rag_system"):
            ok = await mod._discard_partial_vectors(
                doc_id, action="document.process.failed.vector_rollback", reason="exception"
            )

        assert ok is False
        hits = [r for r in caplog.records if "vector_rollback" in r.getMessage()]
        assert hits and hits[0].levelno == logging.ERROR
        assert "severity=reportable" in hits[0].getMessage()


# ---------------------------------------------------------------------------
# 3. 文档删除：顺序与阶段可见性
# ---------------------------------------------------------------------------
class TestDocumentDeleteOrdering:
    @staticmethod
    def _patch(monkeypatch, mod, db, store, minio_error=None, wiki_error=None):
        progress = []
        events = {"completed": 0, "failed": 0}

        @asynccontextmanager
        async def _maker():
            yield db

        async def _get_vector():
            return store

        class _Minio:
            def __init__(self):
                self.calls = []

            async def delete_file_async(self, path):
                self.calls.append(path)
                if minio_error:
                    raise minio_error

        minio = _Minio()

        async def _get_minio():
            return minio

        async def _noop(*args, **kwargs):
            return None

        async def _completed(*args, **kwargs):
            events["completed"] += 1

        async def _failed(*args, **kwargs):
            events["failed"] += 1

        async def _wiki(db_, kb_id, doc_id):
            if wiki_error:
                raise wiki_error

        monkeypatch.setattr(mod, "async_session_maker", _maker)
        monkeypatch.setattr(
            mod, "VectorStoreManager", SimpleNamespace(get_instance=_get_vector)
        )
        monkeypatch.setattr(
            "src.services.minio_service.MinioService",
            SimpleNamespace(get_instance=_get_minio),
        )
        monkeypatch.setattr(
            mod, "update_upload_progress",
            lambda task_id, **kw: progress.append(kw),
        )
        monkeypatch.setattr(mod, "notify_task_progress", _noop)
        monkeypatch.setattr(mod, "notify_task_completed", _completed)
        monkeypatch.setattr(mod, "notify_task_failed", _failed)
        monkeypatch.setattr(mod, "notify_doc_list_changed", _noop)
        monkeypatch.setattr(mod, "_schedule_semantic_cache_invalidation", lambda kb: None)
        monkeypatch.setattr(
            "src.api.knowledge_base.invalidate_kb_list_cache", _noop, raising=False
        )
        monkeypatch.setattr("src.services.wiki_cascade.on_document_deleted", _wiki)
        return SimpleNamespace(progress=progress, events=events, minio=minio)

    @pytest.mark.asyncio
    async def test_vector_failure_keeps_record_and_file(self, monkeypatch, caplog):
        """向量删不掉时不能先删文件：否则「文件没了、记录还在、内容还能搜到」。"""
        from src.services import document_tasks as mod

        doc = SimpleNamespace(id=uuid.uuid4(), owner_id="u-1")
        db = _SeqDB([_Result(one=doc)])
        store = _FakeVectorStore(error=RuntimeError("milvus down"))
        ctx = self._patch(monkeypatch, mod, db, store)

        with caplog.at_level(logging.ERROR, logger="rag_system"):
            await mod.process_document_delete_async(
                str(doc.id), "kb-1", "minio://bucket/a.pdf", "task-1"
            )

        assert db.orm_deleted == [], "向量未删成功却先删了 DB 记录"
        assert ctx.minio.calls == [], "向量未删成功却先删了原文件"
        assert ctx.events["failed"] == 1
        assert ctx.progress[-1]["status"] == "failed"
        assert "vectors" in ctx.progress[-1]["message"]

        hits = [r for r in caplog.records if "action=document.delete.background" in r.getMessage()]
        assert hits, "删除失败必须留下阶段信息"
        assert "phase=vectors" in hits[0].getMessage()
        assert hits[0].levelno == logging.ERROR

    @pytest.mark.asyncio
    async def test_object_cleanup_failure_still_completes(self, monkeypatch, caplog):
        """文件删除只是清理附件，失败不应把已完成的删除改判为失败。"""
        from src.services import document_tasks as mod

        doc = SimpleNamespace(id=uuid.uuid4(), owner_id="u-1")
        db = _SeqDB([_Result(one=doc)])
        store = _FakeVectorStore()
        ctx = self._patch(
            monkeypatch, mod, db, store, minio_error=RuntimeError("s3 down")
        )

        with caplog.at_level(logging.WARNING, logger="rag_system"):
            await mod.process_document_delete_async(
                str(doc.id), "kb-1", "minio://bucket/a.pdf", "task-1"
            )

        assert store.single_deletes == [str(doc.id)]
        assert db.orm_deleted == [doc]
        assert ctx.events["completed"] == 1
        assert ctx.progress[-1]["status"] == "completed"
        hits = [r for r in caplog.records if "action=document.delete.object" in r.getMessage()]
        assert hits, "对象存储残留必须留痕（否则文件永远无人清理）"

    @pytest.mark.asyncio
    async def test_wiki_cascade_failure_does_not_flip_delete_result(
        self, monkeypatch, caplog
    ):
        """文档本身已删干净，级联失败只记 REPORTABLE，不能报「删除失败」。"""
        from src.services import document_tasks as mod

        doc = SimpleNamespace(id=uuid.uuid4(), owner_id="u-1")
        db = _SeqDB([_Result(one=doc)])
        store = _FakeVectorStore()
        ctx = self._patch(
            monkeypatch, mod, db, store, wiki_error=RuntimeError("wiki boom")
        )

        with caplog.at_level(logging.ERROR, logger="rag_system"):
            await mod.process_document_delete_async(
                str(doc.id), "kb-1", "minio://bucket/a.pdf", "task-1"
            )

        assert ctx.events["completed"] == 1
        assert ctx.events["failed"] == 0
        hits = [
            r for r in caplog.records if "action=document.delete.wiki_cascade" in r.getMessage()
        ]
        assert hits and hits[0].levelno == logging.ERROR


# ---------------------------------------------------------------------------
# 4. 知识库删除：按库删「成功但 0 影响」的补偿
# ---------------------------------------------------------------------------
class _KbDB:
    def __init__(self, kb, docs):
        self._kb = kb
        self._docs = docs
        self.deleted_tables = []
        self.commits = 0

    async def execute(self, stmt, *args, **kwargs):
        if isinstance(stmt, Select):
            entity = stmt.column_descriptions[0]["entity"]
            if entity is __import__("src.models", fromlist=["KnowledgeBase"]).KnowledgeBase:
                return _Result(one=self._kb)
            from src.models.wiki_page import WikiPage

            if entity is WikiPage:
                return _Result(rows=[])
            return _Result(rows=self._docs)
        if isinstance(stmt, Delete):
            self.deleted_tables.append(stmt.table.name)
            return _Result()
        return _Result()

    async def commit(self):
        self.commits += 1


class TestKnowledgeBaseVectorResidual:
    @staticmethod
    def _patch(monkeypatch, kb, docs, store):
        async def _get_instance():
            return store

        async def _noop(*args, **kwargs):
            return None

        monkeypatch.setattr(
            "src.api.knowledge_base.manage.VectorStoreManager",
            SimpleNamespace(get_instance=_get_instance),
        )
        monkeypatch.setattr(
            "src.services.minio_service.MinioService",
            SimpleNamespace(get_instance=_noop),
        )
        monkeypatch.setattr(
            "src.api.knowledge_base.manage.invalidate_kb_list_cache", _noop
        )
        monkeypatch.setattr(
            "src.services.wiki_cascade.on_kb_deleted", _noop, raising=False
        )
        monkeypatch.setattr(
            "src.api.knowledge_base.manage._schedule_semantic_cache_invalidation",
            lambda kb_id: None,
        )
        return _KbDB(kb, docs)

    @pytest.mark.asyncio
    async def test_failure_falls_back_to_document_ids(self, monkeypatch, caplog):
        """按 kb_id 删失败时回退逐文档删：历史数据的 kb_id 可能为空串。"""
        from src.api.knowledge_base import delete_knowledge_base

        kb = SimpleNamespace(id=uuid.uuid4(), owner_id="u-1", is_default=False)
        docs = [SimpleNamespace(id=uuid.uuid4(), file_path=None) for _ in range(2)]
        store = _FakeVectorStore(error=RuntimeError("milvus down"))
        db = self._patch(monkeypatch, kb, docs, store)

        with caplog.at_level(logging.ERROR, logger="knowledge_base_api"):
            result = await delete_knowledge_base(
                kb_id=str(kb.id),
                db=db,
                current_user=SimpleNamespace(user_id="u-1", is_authenticated=True),
            )

        assert result == {"message": "知识库已删除"}
        assert "documents" in db.deleted_tables
        hits = [r for r in caplog.records if "action=knowledge_base.delete.vectors" in r.getMessage()]
        assert hits and hits[0].levelno == logging.ERROR, "无主向量必须按 error 级上报"

    @pytest.mark.asyncio
    async def test_residual_after_successful_delete_triggers_retry(self, monkeypatch, caplog):
        """delete 返回成功但仍有残留（kb_id 未回填）→ 按文档重删并核对。"""
        from src.api.knowledge_base import delete_knowledge_base

        kb = SimpleNamespace(id=uuid.uuid4(), owner_id="u-1", is_default=False)
        docs = [SimpleNamespace(id=uuid.uuid4(), file_path=None)]
        # 第一次核对残留 3 条，重删后核对归零
        store = _FakeVectorStore(residual=[3, 0])
        db = self._patch(monkeypatch, kb, docs, store)

        with caplog.at_level(logging.ERROR, logger="knowledge_base_api"):
            await delete_knowledge_base(
                kb_id=str(kb.id),
                db=db,
                current_user=SimpleNamespace(user_id="u-1", is_authenticated=True),
            )

        assert store.batches[0] == [str(kb.id)], "先按 kb_id 删"
        assert store.batches[1] == [str(docs[0].id)], "残留时应回退逐文档删"
        assert not [
            r for r in caplog.records if "vectors_residual" in r.getMessage()
        ], "重删成功不应再报残留"

    @pytest.mark.asyncio
    async def test_unresolved_residual_is_reported(self, monkeypatch, caplog):
        """重删后仍有残留 → REPORTABLE，需人工按 kb_id 清理。"""
        from src.api.knowledge_base import delete_knowledge_base

        kb = SimpleNamespace(id=uuid.uuid4(), owner_id="u-1", is_default=False)
        docs = [SimpleNamespace(id=uuid.uuid4(), file_path=None)]
        store = _FakeVectorStore(residual=[3, 2])
        db = self._patch(monkeypatch, kb, docs, store)

        with caplog.at_level(logging.ERROR, logger="knowledge_base_api"):
            await delete_knowledge_base(
                kb_id=str(kb.id),
                db=db,
                current_user=SimpleNamespace(user_id="u-1", is_authenticated=True),
            )

        hits = [r for r in caplog.records if "action=knowledge_base.delete.vectors_residual" in r.getMessage()]
        assert hits, "残留未被清理必须上报"
        assert "residual=2" in hits[0].getMessage()
        assert hits[0].levelno == logging.ERROR
