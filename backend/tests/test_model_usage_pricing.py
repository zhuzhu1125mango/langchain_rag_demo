"""MODEL-P2.1 成本估算与用量持久化测试。"""

import asyncio

import pytest

from src.models.model_registry import ModelDeployment
from src.services.model_usage import ModelUsageTracker, model_usage


@pytest.fixture(autouse=True)
def _reset_usage():
    model_usage.reset()
    yield
    model_usage.reset()


def test_tracker_estimates_cost_from_prices():
    tracker = ModelUsageTracker()
    tracker.record(
        "p1", "m1", "qwen", "answer", 100, True, 1000, 500,
        input_cost_per_1k=0.01, output_cost_per_1k=0.02,
    )

    row = tracker.snapshot()[0]
    assert row["priced_calls"] == 1
    assert row["unpriced_calls"] == 0
    assert row["estimated_cost"] == pytest.approx(0.02)
    assert row["currency"] == "USD"


def test_tracker_marks_calls_without_prices():
    tracker = ModelUsageTracker()
    tracker.record("p1", "m1", "qwen", "answer", 100, True, 1000, 500)

    row = tracker.snapshot()[0]
    assert row["priced_calls"] == 0
    assert row["unpriced_calls"] == 1
    assert row["estimated_cost"] == 0.0


def test_drain_and_restore_preserve_pending_deltas():
    tracker = ModelUsageTracker()
    tracker.record("p1", "m1", "qwen", "answer", 100, True, 10, 5)

    rows = tracker.drain()
    assert len(rows) == 1
    assert rows[0]["calls"] == 1
    assert rows[0]["provider_id"] == "p1"
    assert rows[0]["cost_currency"] == "USD"
    assert tracker.pending_snapshot() == []

    tracker.restore(rows)
    pending = tracker.pending_snapshot()
    assert len(pending) == 1
    assert pending[0]["calls"] == 1


def test_merge_persisted_and_pending_rows():
    from src.services.model_usage_store import merge_usage_rows

    persisted = [
        {
            "provider_id": "p1",
            "model_id": "m1",
            "model_name": "qwen",
            "task": "answer",
            "calls": 2,
            "errors": 0,
            "input_tokens": 20,
            "output_tokens": 10,
            "total_latency_ms": 400.0,
            "priced_calls": 2,
            "unpriced_calls": 0,
            "estimated_cost": 0.03,
            "currency": "USD",
            "last_error": "",
        }
    ]
    pending = [
        {
            "provider_id": "p1",
            "model_id": "m1",
            "model_name": "qwen",
            "task": "answer",
            "calls": 1,
            "errors": 1,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_latency_ms": 200.0,
            "priced_calls": 0,
            "unpriced_calls": 0,
            "estimated_cost": 0.0,
            "currency": "USD",
            "last_error": "boom",
        }
    ]

    rows = merge_usage_rows(persisted, pending)

    assert len(rows) == 1
    assert rows[0]["calls"] == 3
    assert rows[0]["errors"] == 1
    assert rows[0]["avg_latency_ms"] == 200.0
    assert rows[0]["estimated_cost"] == pytest.approx(0.03)
    assert rows[0]["last_error"] == "boom"


def test_query_usage_totals_maps_persisted_rows():
    from src.services.model_usage_store import query_usage_totals

    class _Mappings:
        def all(self):
            return [
                {
                    "provider_id": "p1",
                    "model_id": "m1",
                    "task": "answer",
                    "cost_currency": "USD",
                    "model_name": "qwen",
                    "calls": 2,
                    "errors": 0,
                    "input_tokens": 20,
                    "output_tokens": 10,
                    "total_latency_ms": 400.0,
                    "priced_calls": 2,
                    "unpriced_calls": 0,
                    "estimated_cost": 0.03,
                }
            ]

    class _Result:
        def mappings(self):
            return _Mappings()

    class _DB:
        async def execute(self, *args, **kwargs):
            return _Result()

    rows = asyncio.run(query_usage_totals(_DB(), days=7))

    assert rows[0]["model_name"] == "qwen"
    assert rows[0]["currency"] == "USD"
    assert rows[0]["total_latency_ms"] == 400.0


def test_flush_usage_deltas_upserts_and_restores():
    import src.services.model_usage_store as store

    class _Session:
        def __init__(self):
            self.rows = {}
            self.commits = 0

        async def get(self, model, key):
            return self.rows.get(key)

        def add(self, obj):
            key = (obj.usage_date, obj.provider_id, obj.model_id, obj.task, obj.cost_currency)
            self.rows[key] = obj

        async def commit(self):
            self.commits += 1

    class _Ctx:
        def __init__(self, session):
            self.session = session

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, *exc):
            return False

    session = _Session()
    original = store.async_session_maker
    store.async_session_maker = lambda: _Ctx(session)
    try:
        model_usage.record(
            "p1", "m1", "qwen", "answer", 100, True, 1000, 500,
            input_cost_per_1k=0.01, output_cost_per_1k=0.02,
        )
        assert asyncio.run(store.flush_usage_deltas()) == 1

        stored = next(iter(session.rows.values()))
        assert stored.calls == 1
        assert stored.estimated_cost == pytest.approx(0.02)
        assert model_usage.pending_snapshot() == []

        model_usage.record("p1", "m1", "qwen", "answer", 100, True, 10, 5)
        assert asyncio.run(store.flush_usage_deltas()) == 1
        assert stored.calls == 2
        assert stored.input_tokens == 1010
    finally:
        store.async_session_maker = original


def test_flush_failure_restores_deltas():
    import src.services.model_usage_store as store

    class _BrokenCtx:
        async def __aenter__(self):
            raise RuntimeError("db down")

        async def __aexit__(self, *exc):
            return False

    original = store.async_session_maker
    store.async_session_maker = lambda: _BrokenCtx()
    try:
        model_usage.record("p1", "m1", "qwen", "answer", 100, True, 10, 5)
        assert asyncio.run(store.flush_usage_deltas()) == 0
        assert len(model_usage.pending_snapshot()) == 1
    finally:
        store.async_session_maker = original


def test_model_config_carries_pricing_and_rejects_negative():
    from src.services.model_config_service import ModelConfigError, _model_config, _price_value

    row = ModelDeployment(
        id="m1",
        provider_id="p1",
        model_name="gpt-test",
        display_name="外部模型",
        capabilities=["chat"],
        context_window=8192,
        max_output_tokens=4096,
        embedding_dimension=0,
        input_cost_per_1k=0.001,
        output_cost_per_1k=0.002,
        price_currency="usd",
        enabled=True,
    )
    config = _model_config(row)
    assert config.input_cost_per_1k == 0.001
    assert config.output_cost_per_1k == 0.002
    assert config.price_currency == "usd"

    with pytest.raises(ModelConfigError):
        _price_value("input_cost_per_1k", -1)


@pytest.mark.asyncio
async def test_chat_model_group_estimates_cost_from_target_pricing():
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import RunnableLambda

    from src.services.model_adapters import ChatModelGroup

    model = type(
        "M",
        (),
        {
            "id": "m1",
            "model": "qwen",
            "input_cost_per_1k": 0.01,
            "output_cost_per_1k": 0.02,
            "price_currency": "USD",
        },
    )()
    target = type("T", (), {"provider": type("P", (), {"id": "p1"})(), "model": model})()
    result = AIMessage(
        content="ok",
        usage_metadata={"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500},
    )

    group = ChatModelGroup([RunnableLambda(lambda _: result)], ["p1/m1"], [target], "answer")
    await group.ainvoke("hello")

    row = model_usage.snapshot()[0]
    assert row["priced_calls"] == 1
    assert row["estimated_cost"] == pytest.approx(0.02)
