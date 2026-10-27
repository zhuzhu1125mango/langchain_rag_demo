"""ExperimentManager 单元测试（SEC-15 覆盖率补测）。

用假 AsyncSession 覆盖实验生命周期、归属过滤、分流与结果分析分支，
不依赖真实 PostgreSQL（集成版用例仍在 test_experiment.py）。
"""

import uuid
from datetime import datetime
from types import SimpleNamespace

from src.services import experiment_manager as em_module
from src.services.experiment_manager import ExperimentManager, _owner_condition


class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeResult:
    def __init__(self, scalar_one=None, scalars=None):
        self._scalar_one = scalar_one
        self._scalars = scalars or []

    def scalar_one_or_none(self):
        return self._scalar_one

    def scalars(self):
        return _FakeScalars(self._scalars)


class _FakeSession:
    def __init__(self, results=None):
        self.added = []
        self.commits = 0
        self.deleted = []
        self.executed = []
        self._results = list(results or [])

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()

    async def execute(self, statement, params=None):
        self.executed.append(statement)
        return self._results.pop(0) if self._results else _FakeResult()

    async def delete(self, obj):
        self.deleted.append(obj)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _SessionQueue:
    """按调用顺序返回预设会话，模拟同一方法内的多次 async_session()。"""

    def __init__(self, *sessions):
        self._sessions = list(sessions)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self._sessions.pop(0)


def _patch(monkeypatch, *sessions):
    queue = _SessionQueue(*sessions)
    monkeypatch.setattr(em_module, "async_session", queue)
    return queue


def _experiment(**overrides):
    values = {
        "id": uuid.uuid4(),
        "name": "实验A",
        "description": "描述",
        "status": "created",
        "owner_id": "u-1",
        "variants": [{"id": "v1", "name": "A", "weight": 1.0, "config": {}}],
        "metrics": ["accuracy"],
        "traffic_allocation": {},
        "created_at": datetime(2026, 10, 27, 10, 0, 0),
        "started_at": None,
        "ended_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_owner_condition_switches_legacy_visibility():
    strict = _owner_condition("u-1")
    with_legacy = _owner_condition("u-1", include_legacy=True)
    assert str(strict) != str(with_legacy)
    assert "owner_id" in str(strict)


async def test_create_experiment_adds_variants(monkeypatch):
    session = _FakeSession()
    _patch(monkeypatch, session)
    experiment_id = await ExperimentManager().create_experiment(
        "实验A", "描述", variants=[{"name": "A", "weight": 1.0}], metrics=["accuracy"], owner_id="u-1"
    )
    assert experiment_id
    assert session.added[0].name == "实验A"
    assert session.added[0].owner_id == "u-1"
    assert len(session.added) == 2, "实验 + 1 个变体"
    assert session.commits == 2


async def test_create_experiment_defaults(monkeypatch):
    session = _FakeSession()
    _patch(monkeypatch, session)
    await ExperimentManager().create_experiment("空实验")
    assert session.added[0].metrics == ["accuracy", "response_time"]
    assert len(session.added) == 1


async def test_start_experiment_states(monkeypatch):
    created = _experiment()
    _patch(monkeypatch, _FakeSession([_FakeResult(scalar_one=created)]))
    manager = ExperimentManager()
    assert await manager.start_experiment(str(created.id), owner_id="u-1") is True
    assert created.status == "running"
    assert manager.active_experiments[str(created.id)] is created

    running = _experiment(status="running")
    _patch(monkeypatch, _FakeSession([_FakeResult(scalar_one=running)]))
    assert await ExperimentManager().start_experiment(str(running.id), owner_id="u-1") is False

    _patch(monkeypatch, _FakeSession([_FakeResult(scalar_one=None)]))
    assert await ExperimentManager().start_experiment("missing", owner_id="u-1") is False


async def test_stop_experiment_states(monkeypatch):
    running = _experiment(status="running")
    manager = ExperimentManager()
    manager.active_experiments[str(running.id)] = running
    _patch(monkeypatch, _FakeSession([_FakeResult(scalar_one=running)]))
    assert await manager.stop_experiment(str(running.id), owner_id="u-1") is True
    assert running.status == "stopped"
    assert str(running.id) not in manager.active_experiments

    _patch(monkeypatch, _FakeSession([_FakeResult(scalar_one=_experiment())]))
    assert await ExperimentManager().stop_experiment("exp", owner_id="u-1") is False


async def test_get_experiment_and_list(monkeypatch):
    experiment = _experiment(
        started_at=datetime(2026, 10, 27, 11, 0, 0), ended_at=None
    )
    _patch(monkeypatch, _FakeSession([_FakeResult(scalar_one=experiment)]))
    result = await ExperimentManager().get_experiment(str(experiment.id), owner_id="u-1")
    assert result["started_at"] == "2026-10-27T11:00:00"
    assert result["ended_at"] is None
    assert result["status"] == "created"

    _patch(monkeypatch, _FakeSession([_FakeResult(scalar_one=None)]))
    assert await ExperimentManager().get_experiment("missing", owner_id="u-1") is None

    rows = [_experiment(), _experiment(name="实验B", status="running")]
    _patch(monkeypatch, _FakeSession([_FakeResult(scalars=rows)]))
    listing = await ExperimentManager().list_experiments(status="running", owner_id="u-1")
    assert [e["name"] for e in listing] == ["实验A", "实验B"]


async def test_delete_experiments_empty_invalid_and_mixed(monkeypatch):
    manager = ExperimentManager()
    empty = await manager.delete_experiments([])
    assert empty["message"] == "未提供实验ID"

    valid = _experiment()
    manager.active_experiments[str(valid.id)] = valid
    session = _FakeSession([_FakeResult(scalars=[valid])])
    _patch(monkeypatch, session)
    result = await manager.delete_experiments(
        [str(valid.id), "not-a-uuid", str(uuid.uuid4())], owner_id="u-1"
    )
    assert result["success_count"] == 1
    assert result["failed_count"] == 2
    assert {f["reason"] for f in result["failed"]} == {"实验ID格式无效", "实验不存在"}
    assert session.deleted == [valid]
    assert str(valid.id) not in manager.active_experiments
    assert len(session.executed) == 5, "1 次查询 + 4 次关联表清理"


def test_hash_user_id_is_deterministic_and_normalized():
    manager = ExperimentManager()
    first = manager._hash_user_id("u-1", "exp-1")
    assert 0.0 <= first <= 1.0
    assert first == manager._hash_user_id("u-1", "exp-1")


async def test_allocate_traffic_cached_and_new(monkeypatch):
    manager = ExperimentManager()
    manager.active_experiments["exp-1"] = {
        "status": "running",
        "variants": [{"id": "v1", "weight": 1.0}],
    }
    session = _FakeSession()
    _patch(monkeypatch, session)
    variant_id = await manager.allocate_traffic("exp-1", "u-1", "s-1", owner_id="u-1")
    assert variant_id == "v1"
    assert session.added[0].variant_id == "v1"

    # 未缓存：get_experiment 非 running → None
    manager2 = ExperimentManager()
    _patch(monkeypatch, _FakeSession([_FakeResult(scalar_one=_experiment(status="created"))]))
    assert await manager2.allocate_traffic("exp-2", "u-1", owner_id="u-1") is None

    # 未缓存且无变体：缓存命中但 variants 为空 → None
    manager3 = ExperimentManager()
    manager3.active_experiments["exp-3"] = {"status": "running", "variants": []}
    assert await manager3.allocate_traffic("exp-3", "u-1", owner_id="u-1") is None


async def test_allocate_traffic_for_chat(monkeypatch):
    manager = ExperimentManager()
    assert await manager.allocate_traffic_for_chat("not-a-uuid", "u-1") is None

    _patch(monkeypatch, _FakeSession([_FakeResult(scalar_one=_experiment(status="created"))]))
    assert await manager.allocate_traffic_for_chat(str(uuid.uuid4()), "u-1") is None

    _patch(
        monkeypatch,
        _FakeSession(
            [
                _FakeResult(
                    scalar_one=_experiment(
                        status="running",
                        variants=[{"id": "v1", "name": "A", "weight": 1.0, "config": {"k": 1}}],
                    )
                )
            ]
        ),
        _FakeSession(),
    )
    result = await manager.allocate_traffic_for_chat(str(uuid.uuid4()), "u-1")
    assert result["variant_id"] == "v1"
    assert result["config"] == {"k": 1}


async def test_get_experiment_any(monkeypatch):
    manager = ExperimentManager()
    assert await manager._get_experiment_any("bad-uuid") is None

    _patch(monkeypatch, _FakeSession([_FakeResult(scalar_one=None)]))
    assert await manager._get_experiment_any(str(uuid.uuid4())) is None

    _patch(monkeypatch, _FakeSession([_FakeResult(scalar_one=_experiment())]))
    result = await manager._get_experiment_any(str(uuid.uuid4()))
    assert result["name"] == "实验A"


async def test_record_metric_guards_and_writes(monkeypatch):
    manager = ExperimentManager()

    async def _missing(*args, **kwargs):
        return None

    monkeypatch.setattr(manager, "get_experiment", _missing)
    session = _FakeSession()
    _patch(monkeypatch, session)
    await manager.record_metric("exp", "v1", "accuracy", 1.0, owner_id="u-1")
    assert session.added == []

    async def _found(*args, **kwargs):
        return {"id": "exp"}

    monkeypatch.setattr(manager, "get_experiment", _found)
    session = _FakeSession()
    _patch(monkeypatch, session)
    await manager.record_metric("exp", "v1", "accuracy", 1.0, owner_id="u-1")
    assert session.added[0].metric_name == "accuracy"

    chat_session = _FakeSession()
    _patch(monkeypatch, chat_session)
    await manager.record_metric_for_chat("exp", "v1", "accuracy", 0.5)
    assert chat_session.added[0].metric_value == 0.5


async def test_get_metrics_groups_by_variant(monkeypatch):
    manager = ExperimentManager()

    async def _missing(*args, **kwargs):
        return None

    monkeypatch.setattr(manager, "get_experiment", _missing)
    assert await manager.get_metrics("exp") == {}

    async def _found(*args, **kwargs):
        return {"id": "exp"}

    monkeypatch.setattr(manager, "get_experiment", _found)
    rows = [
        SimpleNamespace(variant_id="v1", metric_name="accuracy", metric_value=0.9),
        SimpleNamespace(variant_id="v1", metric_name="accuracy", metric_value=0.7),
        SimpleNamespace(variant_id="v1", metric_name="latency", metric_value=100),
        SimpleNamespace(variant_id="v2", metric_name="accuracy", metric_value=0.5),
    ]
    _patch(monkeypatch, _FakeSession([_FakeResult(scalars=rows)]))
    metrics = await manager.get_metrics("exp", owner_id="u-1")
    assert metrics["v1"]["accuracy"] == [0.9, 0.7]
    assert metrics["v1"]["latency"] == [100]
    assert metrics["v2"]["accuracy"] == [0.5]


async def test_analyze_experiment_paths(monkeypatch):
    manager = ExperimentManager()

    async def _missing(*args, **kwargs):
        return None

    monkeypatch.setattr(manager, "get_experiment", _missing)
    no_data = await manager.analyze_experiment("exp")
    assert no_data["status"] == "no_data"

    async def _found(*args, **kwargs):
        return {"id": "exp", "variants": [{"id": "v1", "name": "A"}]}

    async def _no_metrics(*args, **kwargs):
        return {}

    monkeypatch.setattr(manager, "get_experiment", _found)
    monkeypatch.setattr(manager, "get_metrics", _no_metrics)
    assert (await manager.analyze_experiment(str(uuid.uuid4())))["status"] == "no_data"

    async def _metrics(*args, **kwargs):
        return {
            "v1": {"accuracy": [0.9, 0.7], "latency": [100, 120]},
            "v2": {"accuracy": [0.5]},
        }

    monkeypatch.setattr(manager, "get_metrics", _metrics)
    session = _FakeSession()
    _patch(monkeypatch, session)
    analysis = await manager.analyze_experiment(str(uuid.uuid4()))
    assert analysis["status"] == "completed"
    assert analysis["winner_variant_id"] == "v1"
    assert analysis["confidence_level"] == 0.85
    assert analysis["variant_results"][0]["variant_name"] == "A"
    assert analysis["variant_results"][0]["sample_size"] == 2
    assert session.added[0].winning_variant_id == "v1"


async def test_get_experiment_result_formats(monkeypatch):
    manager = ExperimentManager()

    async def _missing(*args, **kwargs):
        return None

    monkeypatch.setattr(manager, "get_experiment", _missing)
    assert await manager.get_experiment_result("exp") is None

    async def _found(*args, **kwargs):
        return {"id": "exp"}

    monkeypatch.setattr(manager, "get_experiment", _found)
    analyzed_at = datetime(2026, 10, 27, 12, 0, 0)
    new_format = SimpleNamespace(
        experiment_id="exp",
        winning_variant_id="v1",
        analysis_data={"variant_results": [{"variant_id": "v1"}]},
        confidence=0.85,
        analyzed_at=analyzed_at,
    )
    _patch(monkeypatch, _FakeSession([_FakeResult(scalar_one=new_format)]))
    result = await manager.get_experiment_result("exp", owner_id="u-1")
    assert result["variant_results"] == [{"variant_id": "v1"}]
    assert result["winner_variant_id"] == "v1"
    assert result["analyzed_at"] == analyzed_at.isoformat()

    old_format = SimpleNamespace(
        experiment_id="exp",
        winning_variant_id=None,
        analysis_data={"v1": {"accuracy": {"average": 0.8, "count": 3}, "note": "ignore"}},
        confidence=0.0,
        analyzed_at=analyzed_at,
    )
    _patch(monkeypatch, _FakeSession([_FakeResult(scalar_one=old_format)]))
    result = await manager.get_experiment_result("exp", owner_id="u-1")
    assert result["variant_results"][0]["metrics"]["accuracy"] == {"mean": 0.8, "std": 0.0, "count": 3}
    assert result["winner_variant_id"] is None

    _patch(monkeypatch, _FakeSession([_FakeResult(scalar_one=None)]))
    assert await manager.get_experiment_result("exp", owner_id="u-1") is None
