"""Provider 熔断与模型用量统计测试（MODEL-P2.1）。"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from src.services.model_adapters import ChatModelGroup
from src.services.model_health import ProviderHealthTracker
from src.services.model_usage import ModelUsageTracker


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_circuit_opens_after_threshold_and_recovers_after_cooldown():
    clock = FakeClock()
    tracker = ProviderHealthTracker(failure_threshold=3, cooldown_seconds=60, clock=clock)

    for _ in range(3):
        tracker.record_failure("p1", RuntimeError("down"))

    assert tracker.state("p1") == "open"
    assert tracker.is_available("p1") is False

    clock.advance(60)
    assert tracker.state("p1") == "half_open"
    assert tracker.is_available("p1") is True
    # 半开只放行一次探测
    assert tracker.is_available("p1") is False

    tracker.record_success("p1")
    assert tracker.state("p1") == "closed"
    assert tracker.is_available("p1") is True
    assert tracker.snapshot()[0]["total_skipped"] >= 2


def test_half_open_probe_failure_reopens_circuit():
    clock = FakeClock()
    tracker = ProviderHealthTracker(failure_threshold=2, cooldown_seconds=30, clock=clock)
    tracker.record_failure("p1", RuntimeError("down"))
    tracker.record_failure("p1", RuntimeError("down"))
    clock.advance(30)

    assert tracker.is_available("p1") is True
    tracker.record_failure("p1", RuntimeError("still down"))

    assert tracker.state("p1") == "open"
    clock.advance(29)
    assert tracker.is_available("p1") is False


def _fake_target(provider_id: str, model_id: str = "m1", model_name: str = "fake-model"):
    provider = type("P", (), {"id": provider_id})()
    model = type("M", (), {"id": model_id, "model": model_name})()
    return type("T", (), {"provider": provider, "model": model})()


@pytest.fixture(autouse=True)
def _reset_trackers():
    from src.services.model_health import provider_health
    from src.services.model_usage import model_usage

    provider_health.reset()
    model_usage.reset()
    yield
    provider_health.reset()
    model_usage.reset()


@pytest.mark.asyncio
async def test_group_skips_open_provider_and_records_usage():
    from src.services.model_health import provider_health
    from src.services.model_usage import model_usage

    primary = RunnableLambda(lambda _: AIMessage(content="primary"))
    fallback = RunnableLambda(lambda _: AIMessage(content="fallback", usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}))
    group = ChatModelGroup(
        [primary, fallback],
        ["p1/m1", "p2/m2"],
        [_fake_target("p1", "m1"), _fake_target("p2", "m2", "backup")],
        "answer",
    )

    for _ in range(3):
        provider_health.record_failure("p1", RuntimeError("down"))

    result = await group.ainvoke("hello")

    assert result.content == "fallback"
    rows = model_usage.snapshot()
    assert len(rows) == 1
    assert rows[0]["provider_id"] == "p2"
    assert rows[0]["calls"] == 1
    assert rows[0]["input_tokens"] == 7
    assert rows[0]["output_tokens"] == 3
    assert rows[0]["task"] == "answer"


@pytest.mark.asyncio
async def test_group_records_failure_and_falls_back():
    from src.services.model_usage import model_usage

    def boom(_):
        raise RuntimeError("primary down")

    group = ChatModelGroup(
        [RunnableLambda(boom), RunnableLambda(lambda _: AIMessage(content="ok"))],
        ["p1/m1", "p2/m2"],
        [_fake_target("p1", "m1"), _fake_target("p2", "m2")],
        "fast",
    )

    result = await group.ainvoke("hello")

    assert result.content == "ok"
    rows = {(r["provider_id"], r["task"]): r for r in model_usage.snapshot()}
    assert rows[("p1", "fast")]["errors"] == 1
    assert rows[("p2", "fast")]["calls"] == 1


@pytest.mark.asyncio
async def test_group_raises_circuit_error_when_all_providers_open():
    from src.services.model_health import ProviderCircuitError, provider_health

    group = ChatModelGroup(
        [RunnableLambda(lambda _: "never")],
        ["p1/m1"],
        [_fake_target("p1", "m1")],
        "answer",
    )
    for _ in range(3):
        provider_health.record_failure("p1", RuntimeError("down"))

    with pytest.raises(ProviderCircuitError):
        await group.ainvoke("hello")


def test_usage_tracker_aggregates_by_provider_model_task():
    tracker = ModelUsageTracker()
    tracker.record("p1", "m1", "qwen", "answer", 100, True, 10, 5)
    tracker.record("p1", "m1", "qwen", "answer", 300, True, 20, 10)
    tracker.record("p1", "m1", "qwen", "answer", 200, False, error="boom")

    row = tracker.snapshot()[0]
    assert row["calls"] == 3
    assert row["errors"] == 1
    assert row["input_tokens"] == 30
    assert row["output_tokens"] == 15
    assert row["avg_latency_ms"] == 200.0
