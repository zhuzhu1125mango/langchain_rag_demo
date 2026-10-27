"""SampleStore 单元测试（SEC-15 覆盖率补测）。

用假 AsyncSession 驱动全部读写/统计分支，不依赖真实 PostgreSQL。
"""

import uuid
from datetime import datetime
from types import SimpleNamespace

from src.services import sample_store as sample_store_module
from src.services.sample_store import SampleStore


class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeResult:
    def __init__(self, scalar_one=None, scalars=None, rows=None):
        self._scalar_one = scalar_one
        self._scalars = scalars or []
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._scalar_one

    def scalars(self):
        return _FakeScalars(self._scalars)

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, results=None):
        self.added = []
        self.commits = 0
        self.deleted = []
        self._results = list(results or [])
        self.last_result = None

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()

    async def execute(self, statement, params=None):
        self.last_result = self._results.pop(0) if self._results else _FakeResult()
        return self.last_result

    async def delete(self, obj):
        self.deleted.append(obj)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


def _patch(monkeypatch, session):
    monkeypatch.setattr(sample_store_module, "async_session", lambda: session)
    return session


def _execution(**overrides):
    values = {
        "id": uuid.uuid4(),
        "session_id": "s-1",
        "user_id": "u-1",
        "question": "问题",
        "final_decision": True,
        "final_confidence": 0.9,
        "strategy_results": {"rule": 0.8},
        "used_knowledge_base": True,
        "created_at": datetime(2026, 10, 27, 10, 0, 0),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _feedback(**overrides):
    values = {
        "id": uuid.uuid4(),
        "execution_id": "e-1",
        "strategy_name": "rule",
        "feedback_score": 0.5,
        "reason": "reason",
        "created_at": datetime(2026, 10, 27, 10, 0, 0),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def test_save_execution_defaults_legacy_owner_and_returns_id(monkeypatch):
    session = _patch(monkeypatch, _FakeSession())
    execution_id = await SampleStore().save_execution(
        "s-1", "问题", True, 0.9, {"rule": 0.8}, True, user_id=None
    )
    assert execution_id
    saved = session.added[0]
    assert saved.user_id == "__legacy__"
    assert session.commits == 1


async def test_save_feedback(monkeypatch):
    session = _patch(monkeypatch, _FakeSession())
    await SampleStore().save_feedback("e-1", "rule", 1.0, "很好")
    assert session.added[0].execution_id == "e-1"
    assert session.added[0].feedback_score == 1.0
    assert session.commits == 1


async def test_get_execution_found_and_missing(monkeypatch):
    execution = _execution()
    _patch(monkeypatch, _FakeSession([_FakeResult(scalar_one=execution)]))
    result = await SampleStore().get_execution("e-1")
    assert result["id"] == str(execution.id)
    assert result["created_at"] == execution.created_at.isoformat()

    _patch(monkeypatch, _FakeSession([_FakeResult(scalar_one=None)]))
    assert await SampleStore().get_execution("missing") is None


async def test_get_executions_by_session_and_recent(monkeypatch):
    rows = [_execution(), _execution(question="问题2")]
    _patch(monkeypatch, _FakeSession([_FakeResult(scalars=rows)]))
    results = await SampleStore().get_executions_by_session("s-1", owner_id="u-1")
    assert [r["question"] for r in results] == ["问题", "问题2"]

    _patch(monkeypatch, _FakeSession([_FakeResult(scalars=rows)]))
    recent = await SampleStore().get_recent_executions(limit=10, owner_id="u-1")
    assert recent[0]["user_id"] == "u-1"


async def test_get_executions_with_feedback_merges_rows(monkeypatch):
    exec1 = _execution()
    rows = [
        (exec1, _feedback(execution_id=str(exec1.id))),
        (exec1, None),
        (_execution(question="问题2"), _feedback(feedback_score=-1.0)),
    ]
    _patch(monkeypatch, _FakeSession([_FakeResult(rows=rows)]))
    results = await SampleStore().get_executions_with_feedback(limit=10)
    assert len(results) == 2
    assert results[0]["id"] == str(exec1.id)
    assert results[0]["feedbacks"][0]["strategy_name"] == "rule"
    assert results[1]["feedbacks"][0]["feedback_score"] == -1.0


async def test_get_feedback_for_execution(monkeypatch):
    _patch(monkeypatch, _FakeSession([_FakeResult(scalars=[_feedback()])]))
    results = await SampleStore().get_feedback_for_execution("e-1")
    assert results[0]["reason"] == "reason"


async def test_strategy_feedback_stats_empty_and_values(monkeypatch):
    _patch(monkeypatch, _FakeSession([_FakeResult(scalars=[])]))
    assert await SampleStore().get_strategy_feedback_stats("rule") == {
        "count": 0,
        "average_score": 0.0,
        "positive_count": 0,
        "negative_count": 0,
    }

    rows = [_feedback(feedback_score=1.0), _feedback(feedback_score=-1.0), _feedback(feedback_score=0.0)]
    _patch(monkeypatch, _FakeSession([_FakeResult(scalars=rows)]))
    stats = await SampleStore().get_strategy_feedback_stats("rule", owner_id="u-1")
    assert stats == {
        "count": 3,
        "average_score": 0.0,
        "positive_count": 1,
        "negative_count": 1,
    }


async def test_execution_stats_empty_and_values(monkeypatch):
    _patch(monkeypatch, _FakeSession([_FakeResult(scalars=[])]))
    assert await SampleStore().get_execution_stats() == {
        "total_count": 0,
        "knowledge_base_used_count": 0,
        "average_confidence": 0.0,
        "daily_counts": {},
    }

    rows = [
        _execution(final_confidence=1.0, used_knowledge_base=True),
        _execution(
            final_confidence=0.5,
            used_knowledge_base=False,
            created_at=datetime(2026, 10, 27, 11, 0, 0),
        ),
        _execution(
            final_confidence=0.0,
            used_knowledge_base=False,
            created_at=datetime(2026, 10, 26, 11, 0, 0),
        ),
    ]
    _patch(monkeypatch, _FakeSession([_FakeResult(scalars=rows)]))
    stats = await SampleStore().get_execution_stats(owner_id="u-1")
    assert stats["total_count"] == 3
    assert stats["knowledge_base_used_count"] == 1
    assert stats["average_confidence"] == 0.5
    assert stats["daily_counts"] == {"2026-10-27": 2, "2026-10-26": 1}


async def test_update_and_get_strategy_config(monkeypatch):
    existing = SimpleNamespace(
        strategy_name="rule",
        weight=0.5,
        enabled=True,
        config={},
        updated_at=datetime(2026, 10, 27),
    )
    session = _patch(monkeypatch, _FakeSession([_FakeResult(scalar_one=existing)]))
    await SampleStore().update_strategy_config("rule", {"weight": 0.7, "enabled": False})
    assert existing.weight == 0.7
    assert existing.enabled is False
    assert session.commits == 1
    assert session.added == []

    session = _patch(monkeypatch, _FakeSession([_FakeResult(scalar_one=None)]))
    await SampleStore().update_strategy_config("new", {"weight": 0.2})
    assert session.added[0].strategy_name == "new"

    _patch(monkeypatch, _FakeSession([_FakeResult(scalar_one=existing)]))
    config = await SampleStore().get_strategy_config("rule")
    assert config["updated_at"] == existing.updated_at.isoformat()

    _patch(monkeypatch, _FakeSession([_FakeResult(scalar_one=None)]))
    assert await SampleStore().get_strategy_config("missing") is None


async def test_delete_old_executions_commits(monkeypatch):
    session = _patch(monkeypatch, _FakeSession([_FakeResult()]))
    await SampleStore().delete_old_executions(days_to_keep=7)
    assert session.commits == 1
    assert session.last_result is not None
