"""SEC-4：文档状态/移动 KB 与向量、缓存一致性。

CODE-1 拆分后：`apply_document_status` 及索引/缓存辅助已移入
`services.document_tasks`，端点仍在 `api.document`；两层的打桩目标各自独立。
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from src.api import document as doc_api  # noqa: F401 (保留包级引用，便于后续包级断言)
from src.api.document import DocumentUpdate, update_document
from src.api.document import manage as doc_manage
from src.services import document_tasks as doc_tasks
from src.services.document_tasks import apply_document_status


class FakeDB:
    def __init__(self):
        self.commit_calls = 0

    async def commit(self):
        self.commit_calls += 1


def _doc(**overrides):
    base = dict(
        id=uuid.uuid4(),
        owner_id="u1",
        filename="a.txt",
        file_path="minio://documents/a.txt",
        size=10,
        kb_id=uuid.uuid4(),
        status="published",
        processing_status="completed",
        processing_message="",
        processing_progress=100,
        chunks_count=3,
        tags=[],
        category_id=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_unpublish_deletes_vectors_and_cache(monkeypatch):
    doc = _doc()
    db = FakeDB()
    delete = AsyncMock()
    invalidate = Mock()
    monkeypatch.setattr(doc_tasks, "delete_document_vectors", delete)
    monkeypatch.setattr(doc_tasks, "invalidate_doc_cache", invalidate)

    await apply_document_status(db, doc, "draft", None)

    delete.assert_awaited_once_with(str(doc.id))
    assert doc.status == "draft"
    assert doc.chunks_count == 0
    assert db.commit_calls == 1
    invalidate.assert_called()


@pytest.mark.asyncio
async def test_publish_schedules_reindex(monkeypatch):
    doc = _doc(status="draft", processing_status="failed", chunks_count=0)
    db = FakeDB()
    delete = AsyncMock()
    schedule = Mock()
    monkeypatch.setattr(doc_tasks, "delete_document_vectors", delete)
    monkeypatch.setattr(doc_tasks, "schedule_document_reindex", schedule)
    monkeypatch.setattr(doc_tasks, "invalidate_doc_cache", Mock())

    await apply_document_status(db, doc, "published", None)

    assert doc.processing_status == "processing"
    assert db.commit_calls == 1
    schedule.assert_called_once_with(doc, None)


@pytest.mark.asyncio
async def test_status_change_rejected_while_processing():
    doc = _doc(status="draft", processing_status="processing", chunks_count=0)
    with pytest.raises(HTTPException) as exc:
        await apply_document_status(FakeDB(), doc, "published", None)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_move_published_document_reindexes_under_new_kb(monkeypatch):
    doc = _doc()
    old_kb = doc.kb_id
    new_kb = uuid.uuid4()
    db = FakeDB()

    monkeypatch.setattr(doc_manage, "_get_owned_document", AsyncMock(return_value=doc))
    monkeypatch.setattr(doc_manage, "validate_kb_ownership", AsyncMock())
    delete = AsyncMock()
    schedule = Mock()
    invalidate = Mock()
    # 端点实现位于 api.document.manage（CODE-1 拆分），打桩目标随之迁移
    monkeypatch.setattr(doc_manage, "delete_document_vectors", delete)
    monkeypatch.setattr(doc_manage, "schedule_document_reindex", schedule)
    monkeypatch.setattr(doc_manage, "invalidate_doc_cache", invalidate)

    user = SimpleNamespace(user_id="u1", is_authenticated=True)
    await update_document(str(doc.id), DocumentUpdate(kb_id=str(new_kb)), None, db=db, current_user=user)

    delete.assert_awaited_once_with(str(doc.id))
    assert doc.kb_id == new_kb
    assert doc.processing_status == "processing"
    schedule.assert_called_once()
    invalidate.assert_called_with(str(old_kb), str(new_kb))
    assert db.commit_calls == 1
