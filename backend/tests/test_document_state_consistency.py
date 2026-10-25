"""SEC-4：文档状态/移动 KB 与向量、缓存一致性。"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from src.api import document as doc_api
from src.api.document import DocumentUpdate, _apply_document_status, update_document


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
    monkeypatch.setattr(doc_api, "_delete_document_vectors", delete)
    monkeypatch.setattr(doc_api, "_invalidate_doc_cache", invalidate)

    await _apply_document_status(db, doc, "draft", None)

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
    monkeypatch.setattr(doc_api, "_delete_document_vectors", delete)
    monkeypatch.setattr(doc_api, "_schedule_document_reindex", schedule)
    monkeypatch.setattr(doc_api, "_invalidate_doc_cache", Mock())

    await _apply_document_status(db, doc, "published", None)

    assert doc.processing_status == "processing"
    assert db.commit_calls == 1
    schedule.assert_called_once_with(doc, None)


@pytest.mark.asyncio
async def test_status_change_rejected_while_processing():
    doc = _doc(status="draft", processing_status="processing", chunks_count=0)
    with pytest.raises(HTTPException) as exc:
        await _apply_document_status(FakeDB(), doc, "published", None)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_move_published_document_reindexes_under_new_kb(monkeypatch):
    doc = _doc()
    old_kb = doc.kb_id
    new_kb = uuid.uuid4()
    db = FakeDB()

    monkeypatch.setattr(doc_api, "_get_owned_document", AsyncMock(return_value=doc))
    monkeypatch.setattr(doc_api, "validate_kb_ownership", AsyncMock())
    delete = AsyncMock()
    schedule = Mock()
    monkeypatch.setattr(doc_api, "_delete_document_vectors", delete)
    monkeypatch.setattr(doc_api, "_schedule_document_reindex", schedule)
    invalidate = Mock()
    monkeypatch.setattr(doc_api, "_invalidate_doc_cache", invalidate)

    user = SimpleNamespace(user_id="u1", is_authenticated=True)
    await update_document(str(doc.id), DocumentUpdate(kb_id=str(new_kb)), None, db=db, current_user=user)

    delete.assert_awaited_once_with(str(doc.id))
    assert doc.kb_id == new_kb
    assert doc.processing_status == "processing"
    schedule.assert_called_once()
    invalidate.assert_called_with(str(old_kb), str(new_kb))
    assert db.commit_calls == 1
