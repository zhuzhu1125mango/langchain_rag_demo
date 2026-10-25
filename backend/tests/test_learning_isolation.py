"""SEC-1：学习样本按 owner 隔离与写入归属校验。"""

from unittest.mock import AsyncMock

import pytest

from src.services import sample_store as sample_store_module
from src.services.learning_engine import LearningEngine
from src.services.rule_learner import RuleLearner


@pytest.mark.asyncio
async def test_feedback_rejects_other_owner(monkeypatch):
    engine = LearningEngine()
    monkeypatch.setattr(
        sample_store_module.sample_store,
        "get_execution",
        AsyncMock(return_value={"id": "e1", "user_id": "u1", "strategy_results": {"keyword": 0.8}}),
    )
    save_feedback = AsyncMock()
    monkeypatch.setattr(sample_store_module.sample_store, "save_feedback", save_feedback)

    await engine.record_feedback("e1", 1.0, owner_id="u2", enforce_owner=True)
    save_feedback.assert_not_called()


@pytest.mark.asyncio
async def test_feedback_writes_for_same_owner(monkeypatch):
    engine = LearningEngine()
    monkeypatch.setattr(
        sample_store_module.sample_store,
        "get_execution",
        AsyncMock(return_value={"id": "e1", "user_id": "u1", "strategy_results": {"keyword": 0.8}}),
    )
    save_feedback = AsyncMock()
    monkeypatch.setattr(sample_store_module.sample_store, "save_feedback", save_feedback)

    await engine.record_feedback("e1", 1.0, owner_id="u1", enforce_owner=True)
    save_feedback.assert_awaited_once()


@pytest.mark.asyncio
async def test_extract_training_samples_passes_owner(monkeypatch):
    get_samples = AsyncMock(return_value=[])
    monkeypatch.setattr(sample_store_module.sample_store, "get_executions_with_feedback", get_samples)

    await RuleLearner().extract_training_samples(limit=5, owner_id="u1")
    get_samples.assert_awaited_once_with(5, owner_id="u1")


@pytest.mark.asyncio
async def test_misclassification_analysis_passes_owner(monkeypatch):
    engine = LearningEngine()
    analyze = AsyncMock(return_value=[])
    suggest = AsyncMock(return_value=[])
    monkeypatch.setattr("src.services.learning_engine.rule_learner.analyze_misclassifications", analyze)
    monkeypatch.setattr("src.services.learning_engine.rule_learner.suggest_new_keywords", suggest)

    result = await engine.get_misclassification_analysis(limit=7, owner_id="u1")
    assert result["misclassified_cases"] == []
    analyze.assert_awaited_once_with(7, owner_id="u1")
    suggest.assert_awaited_once_with(7, owner_id="u1")
