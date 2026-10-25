"""系统能力状态接口测试。

验证 /config/info 的 features 字段完整暴露默认关闭能力的开关状态，
避免前端设置页把“已合入但未开启”的能力展示成可用。
"""

import asyncio

from src.api.config import get_system_info
from src.config import settings


EXPECTED_KEYS = {
    "wiki_compile",
    "agent_web_fetch",
    "agent_approval",
    "ab_chat",
    "learning_weight_reload",
    "learning_sample_collect",
    "agent_traj_eval",
}


def _features(info: dict) -> dict:
    return {item["key"]: item for item in info["features"]}


def test_features_cover_all_default_off_capabilities():
    features = _features(asyncio.run(get_system_info()))

    assert set(features) == EXPECTED_KEYS
    for item in features.values():
        assert item["title"]
        assert item["env_var"]
        assert item["note"]
        assert item["scope"] == "admin"
        assert item["risk"] in {"low", "medium", "high"}


def test_features_reflect_runtime_switches(monkeypatch):
    monkeypatch.setattr(settings.wiki_compile, "WIKI_COMPILE_ENABLED", True)
    monkeypatch.setattr(settings.search, "AGENT_WEB_FETCH_ENABLED", False)
    monkeypatch.setattr(settings.guardrail, "AGENT_APPROVAL_MODE", "wait")
    monkeypatch.setattr(settings.experiment, "AB_EXPERIMENT_CHAT_ENABLED", False)
    monkeypatch.setattr(settings.decision, "LEARNING_WEIGHT_RELOAD_ENABLED", True)
    monkeypatch.setattr(settings.decision, "LEARNING_SAMPLE_COLLECT_BG_ENABLED", False)
    monkeypatch.setattr(settings.search, "AGENT_TRAJ_EVAL_ENABLED", True)

    features = _features(asyncio.run(get_system_info()))

    assert features["wiki_compile"]["enabled"] is True
    assert features["agent_web_fetch"]["enabled"] is False
    assert features["agent_approval"]["enabled"] is True
    assert "wait" in features["agent_approval"]["note"]
    assert features["ab_chat"]["enabled"] is False
    assert features["learning_weight_reload"]["enabled"] is True
    assert features["learning_sample_collect"]["enabled"] is False
    assert features["agent_traj_eval"]["enabled"] is True


def test_agent_approval_disabled_when_mode_off(monkeypatch):
    monkeypatch.setattr(settings.guardrail, "AGENT_APPROVAL_MODE", "off")

    approval = _features(asyncio.run(get_system_info()))["agent_approval"]

    assert approval["enabled"] is False
    assert "off" in approval["note"]
