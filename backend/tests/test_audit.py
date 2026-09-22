"""审计日志单元测试（P2-4）。

覆盖：record_audit best-effort（不抛异常）、query_audit_logs 计数/列表、
API 序列化与空结果。采用 FakeDb 内存态，无需真实数据库。
"""

import uuid
from datetime import datetime

import pytest

from src.models.audit_log import AuditLog


def _make_log(**kwargs):
    defaults = dict(
        id=str(uuid.uuid4()),
        user_id="u1",
        username="alice",
        action="document.upload",
        resource_type="document",
        resource_id="doc-1",
        detail={"filename": "a.pdf"},
        ip_address="127.0.0.1",
        created_at=datetime(2026, 9, 22, 12, 0, 0),
    )
    defaults.update(kwargs)
    return AuditLog(**defaults)


class FakeScalarsResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items


class FakeDb:
    """假 AsyncSession：scalar() 返回计数，execute() 返回列表。"""

    def __init__(self, logs):
        self.logs = logs

    async def scalar(self, stmt):
        return len(self.logs)

    async def execute(self, stmt):
        return FakeScalarsResult(self.logs)


def test_record_audit_best_effort(monkeypatch):
    """record_audit 在 DB 异常时不应抛出（审计失败不阻断主流程）。"""
    import src.services.audit_service as svc

    class BrokenSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        def add(self, *a):
            raise RuntimeError("boom")

        async def commit(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(svc, "AsyncSessionLocal", lambda: BrokenSession())
    # 不应抛出
    import asyncio
    asyncio.run(svc.record_audit(action="document.upload", resource_type="document"))


@pytest.mark.asyncio
async def test_query_audit_logs_total_and_rows():
    from src.services.audit_service import query_audit_logs

    logs = [_make_log(), _make_log()]
    db = FakeDb(logs)
    rows, total = await query_audit_logs(db, action="document.upload", limit=10)
    assert total == 2
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_query_audit_logs_empty():
    from src.services.audit_service import query_audit_logs

    db = FakeDb([])
    rows, total = await query_audit_logs(db, limit=10)
    assert total == 0
    assert rows == []


@pytest.mark.asyncio
async def test_list_audit_logs_serialization():
    """API 序列化：字段齐全，action 过滤合法返回。"""
    from src.api.audit import list_audit_logs
    from src.auth import CurrentUser

    logs = [_make_log()]
    db = FakeDb(logs)
    user = CurrentUser(is_authenticated=True, user_id="admin")
    resp = await list_audit_logs(
        db=db,
        action="document.upload",
        current_user=user,
        page=1,
        page_size=50,
    )
    assert resp["total"] == 1
    assert len(resp["items"]) == 1
    item = resp["items"][0]
    assert item["action"] == "document.upload"
    assert item["resource_id"] == "doc-1"
    assert item["detail"]["filename"] == "a.pdf"
    assert item["username"] == "alice"
    assert "created_at" in item