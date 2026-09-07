"""链路追踪查询 API 单元测试（P1-2）。

覆盖：列表按用户隔离（默认用户可见 NULL 归属）、分页序列化、
详情归属校验（他人记录返回 404）。
"""

import uuid
from datetime import datetime

import pytest
from fastapi import HTTPException

from src.api.trace import list_traces, get_trace, _trace_user_filter
from src.auth import CurrentUser
from src.models.request_trace import RequestTrace


def _make_trace(user_id, session_id=None, **kwargs):
    """构造内存态 RequestTrace（不落库）。"""
    defaults = dict(
        id=str(uuid.uuid4()),
        question="什么是 RAG？",
        primary_mode="kb_direct",
        total_latency_ms=1234,
        fallback_triggered=False,
        created_at=datetime(2026, 9, 2, 12, 0, 0),
        stages=[{"name": "generate", "status": "done", "latency_ms": 900}],
        token_usage={"prompt_tokens": 100, "completion_tokens": 50, "estimated": False},
    )
    defaults.update(kwargs)
    return RequestTrace(user_id=user_id, session_id=session_id, **defaults)


class FakeScalarsResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items


class FakeDb:
    """假 AsyncSession：支持 scalar（计数）与 execute（列表）、get（详情）。"""

    def __init__(self, traces=None, get_result=None):
        self.traces = traces or []
        self.get_result = get_result

    async def scalar(self, stmt):
        return len(self.traces)

    async def execute(self, stmt):
        return FakeScalarsResult(self.traces)

    async def get(self, model, pk):
        return self.get_result


class TestUserFilter:
    """归属过滤条件测试。"""

    def test_jwt_user_filters_by_own_id(self):
        cond = _trace_user_filter(CurrentUser(user_id="user-a", is_authenticated=True))
        rendered = str(cond.compile(compile_kwargs={"literal_binds": True}))
        assert "user_id" in rendered
        assert "user-a" in rendered

    def test_default_user_includes_null_owner(self):
        cond = _trace_user_filter(CurrentUser(user_id="default", is_authenticated=True))
        rendered = str(cond.compile(compile_kwargs={"literal_binds": True}))
        assert "IS NULL" in rendered


class TestListTraces:
    """列表接口测试。"""

    async def test_returns_total_and_serialized_items(self):
        trace = _make_trace(user_id="user-a", session_id="sess-1")
        db = FakeDb(traces=[trace])
        user = CurrentUser(user_id="user-a", is_authenticated=True)

        resp = await list_traces(
            session_id="sess-1", start=None, end=None, limit=20, offset=0, db=db, current_user=user
        )

        assert resp["total"] == 1
        item = resp["items"][0]
        assert item["question"] == "什么是 RAG？"
        assert item["primary_mode"] == "kb_direct"
        assert item["total_latency_ms"] == 1234
        assert item["stage_count"] == 1
        assert item["token_usage"]["prompt_tokens"] == 100
        assert item["created_at"] == "2026-09-02T12:00:00"


class TestGetTrace:
    """详情接口测试。"""

    async def test_own_trace_returns_detail(self):
        trace = _make_trace(user_id="user-a")
        db = FakeDb(get_result=trace)
        user = CurrentUser(user_id="user-a", is_authenticated=True)

        resp = await get_trace(trace.id, db=db, current_user=user)
        assert resp["id"] == trace.id
        assert resp["stages"] == trace.stages
        assert resp["token_usage"]["estimated"] is False

    async def test_other_users_trace_returns_404(self):
        trace = _make_trace(user_id="user-b")
        db = FakeDb(get_result=trace)
        user = CurrentUser(user_id="user-a", is_authenticated=True)

        with pytest.raises(HTTPException) as exc:
            await get_trace(trace.id, db=db, current_user=user)
        assert exc.value.status_code == 404

    async def test_missing_trace_returns_404(self):
        db = FakeDb(get_result=None)
        user = CurrentUser(user_id="user-a", is_authenticated=True)

        with pytest.raises(HTTPException) as exc:
            await get_trace("no-such-id", db=db, current_user=user)
        assert exc.value.status_code == 404

    async def test_null_owner_visible_only_to_default_user(self):
        trace = _make_trace(user_id=None)
        default_user = CurrentUser(user_id="default", is_authenticated=True)
        jwt_user = CurrentUser(user_id="user-a", is_authenticated=True)

        # 默认用户可见历史 NULL 归属记录
        resp = await get_trace(trace.id, db=FakeDb(get_result=trace), current_user=default_user)
        assert resp["id"] == trace.id

        # JWT 用户不可见
        with pytest.raises(HTTPException) as exc:
            await get_trace(trace.id, db=FakeDb(get_result=trace), current_user=jwt_user)
        assert exc.value.status_code == 404
