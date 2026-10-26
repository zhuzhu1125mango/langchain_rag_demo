"""Provider / Model / Route registry（P1）测试。

不访问真实 Ollama/OpenAI：只验证配置解析、路由解析、能力校验、fallback 策略
与适配器构造。
"""

import asyncio
import json
from pathlib import Path

import pytest

from src.config import settings
from src.services import model_adapters
from src.services.model_manager import ModelManager
from src.services.model_registry import (
    ModelConfig,
    ModelRegistry,
    ModelRegistryError,
    ProviderConfig,
    ResolvedModel,
    ResolvedRoute,
    RouteConfig,
)


def _write_registry(path: Path, *, with_tools: bool = True, embedding_dimension: int = 1024) -> None:
    caps = ["chat", "stream"] + (["tools"] if with_tools else [])
    data = {
        "version": 1,
        "providers": [
            {"id": "local", "type": "ollama", "base_url": "http://127.0.0.1:11434", "external": False},
            {
                "id": "remote",
                "type": "openai_compatible",
                "base_url": "https://api.example.com/v1",
                "api_key": "dummy",
                "external": True,
            },
        ],
        "models": [
            {
                "id": "local-chat",
                "provider": "local",
                "model": "local-chat:1b",
                "capabilities": caps,
                "context_window": 4096,
            },
            {
                "id": "remote-chat",
                "provider": "remote",
                "model": "remote-chat",
                "capabilities": ["chat", "stream", "tools"],
                "context_window": 8192,
            },
            {
                "id": "local-embed",
                "provider": "local",
                "model": "local-embed",
                "capabilities": ["embedding"],
                "embedding_dimension": embedding_dimension,
            },
        ],
        "routes": {
            "answer": {"primary": "local-chat", "fallbacks": ["remote-chat"]},
            "answer_direct": {"primary": "local-chat", "fallbacks": ["remote-chat"]},
            "fast": {"primary": "local-chat"},
            "agent_decision": {"primary": "local-chat", "fallbacks": ["remote-chat"]},
            "embedding": {"primary": "local-embed"},
        },
    }
    path.write_text(json.dumps(data), encoding="utf-8")


def test_legacy_bootstrap_uses_ollama_env(monkeypatch):
    monkeypatch.setattr(settings.model, "OLLAMA_MODEL_NAME", "answer-model")
    monkeypatch.setattr(settings.model, "OLLAMA_DIRECT_MODEL_NAME", "direct-model")
    monkeypatch.setattr(settings.model, "FAST_LLM_MODEL_NAME", "fast-model")
    monkeypatch.setattr(settings.model, "EMBEDDING_MODEL_NAME", "embedding-model")
    monkeypatch.setattr(settings.model, "EMBEDDING_DIMENSION", 1024)

    registry = ModelRegistry.bootstrap_legacy()

    assert registry.get_model_name("answer") == "answer-model"
    assert registry.get_model_name("answer_direct") == "direct-model"
    assert registry.get_model_name("fast") == "fast-model"
    assert registry.get_embedding_model().model.model == "embedding-model"
    assert registry.resolve("answer").primary.provider.type == "ollama"


def test_file_registry_resolves_primary_and_fallback(tmp_path):
    path = tmp_path / "model_registry.json"
    _write_registry(path)
    registry = ModelRegistry.load(str(path))
    registry.routes["answer"].allow_external_fallback = True

    route = registry.resolve("answer")

    assert route.primary.model.id == "local-chat"
    assert route.fallbacks[0].model.id == "remote-chat"
    assert route.primary.provider.is_external() is False
    assert route.fallbacks[0].provider.is_external() is True


def test_external_fallback_requires_opt_in(tmp_path):
    path = tmp_path / "model_registry.json"
    _write_registry(path)
    registry = ModelRegistry.load(str(path))

    blocked = registry.resolve("answer")
    assert [item.model.id for item in blocked.fallbacks] == []

    registry.routes["answer"].allow_external_fallback = True
    allowed = registry.resolve("answer")
    assert [item.model.id for item in allowed.fallbacks] == ["remote-chat"]


def test_agent_decision_requires_tool_capability(tmp_path):
    path = tmp_path / "model_registry.json"
    _write_registry(path, with_tools=False)

    with pytest.raises(ModelRegistryError, match="不支持 tools"):
        ModelRegistry.load(str(path))


def test_embedding_dimension_mismatch_is_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.model, "EMBEDDING_DIMENSION", 1024)
    path = tmp_path / "model_registry.json"
    _write_registry(path, embedding_dimension=768)

    with pytest.raises(ModelRegistryError, match="重建索引"):
        ModelRegistry.load(str(path))


@pytest.mark.asyncio
async def test_chat_model_group_falls_back_on_primary_error():
    from langchain_core.runnables import RunnableLambda

    from src.services.model_adapters import ChatModelGroup

    def boom(_):
        raise RuntimeError("primary down")

    group = ChatModelGroup([RunnableLambda(boom), RunnableLambda(lambda _: "fallback-ok")])

    assert await group.ainvoke("hello") == "fallback-ok"


def test_route_models_dedupe_same_provider_model(monkeypatch):
    provider = type(
        "P",
        (),
        {"id": "p", "type": "openai_compatible", "base_url": "http://localhost:1/v1", "is_external": lambda self: False},
    )()
    model = type(
        "M",
        (),
        {"id": "m", "model": "same", "capabilities": ["chat"], "has": lambda self, cap: cap in self.capabilities},
    )()
    primary = ResolvedModel(model=model, provider=provider, task="answer")
    route = ResolvedRoute(
        task="answer",
        primary=primary,
        fallbacks=[ResolvedModel(model=model, provider=provider, task="answer", fallback=True)],
        config=RouteConfig(primary="m"),
    )

    calls = []

    def fake_build(item, **kwargs):
        calls.append(item.model.id)
        return object()

    monkeypatch.setattr(model_adapters, "build_chat_model", fake_build)
    model_adapters.build_route_models(route, streaming=False, think=None)

    assert calls == ["m"]


def test_openai_adapter_builds_with_key():
    provider = ProviderConfig(
        id="remote",
        type="openai_compatible",
        base_url="https://api.example.com/v1",
        api_key="dummy",
        external=True,
    )
    model = ModelConfig(
        id="remote-chat",
        provider="remote",
        model="gpt-4.1-mini",
        capabilities=["chat", "stream", "tools"],
        max_output_tokens=128,
    )
    llm = model_adapters.build_chat_model(
        ResolvedModel(model=model, provider=provider, task="answer"),
        streaming=True,
        think=False,
    )

    assert llm.model_name == "gpt-4.1-mini"
    assert llm.openai_api_base == "https://api.example.com/v1"


def test_openai_adapter_requires_key():
    provider = type(
        "P",
        (),
        {
            "id": "remote",
            "type": "openai_compatible",
            "base_url": "https://api.example.com/v1",
            "api_key": None,
            "api_key_env": None,
            "api_key_required": True,
            "headers": {},
            "timeout_s": 30,
            "resolved_api_key": lambda self: None,
        },
    )()
    model = type(
        "M",
        (),
        {
            "id": "m",
            "model": "remote-chat",
            "capabilities": ["chat"],
            "has": lambda self, cap: cap in self.capabilities,
            "temperature": None,
            "max_output_tokens": 0,
        },
    )()
    resolved = ResolvedModel(model=model, provider=provider, task="answer")

    with pytest.raises(ModelRegistryError, match="API Key"):
        model_adapters.build_chat_model(resolved, streaming=False, think=False)


def test_startup_validation_accepts_registry_without_ollama_env(tmp_path, monkeypatch):
    """有 registry 文件时，启动校验不应再强制 OLLAMA_* 三项。"""
    path = tmp_path / "model_registry.json"
    _write_registry(path)
    monkeypatch.setattr(settings.model, "MODEL_REGISTRY_FILE", str(path))
    monkeypatch.setattr(settings.model, "OLLAMA_MODEL_NAME", None)
    monkeypatch.setattr(settings.model, "FAST_LLM_MODEL_NAME", None)
    monkeypatch.setattr(settings.model, "EMBEDDING_MODEL_NAME", None)

    from src.main import _validate_model_config

    asyncio.run(_validate_model_config())


def test_model_manager_reloads_registry_file(tmp_path, monkeypatch):
    path = tmp_path / "model_registry.json"
    _write_registry(path)
    monkeypatch.setattr(settings.model, "MODEL_REGISTRY_FILE", str(path))
    manager = ModelManager()

    assert asyncio.run(manager.get_model_for_task("answer")) == "local-chat:1b"

    data = json.loads(path.read_text(encoding="utf-8"))
    data["models"][0]["model"] = "local-chat:2b"
    path.write_text(json.dumps(data), encoding="utf-8")
    manager.reload_registry()

    assert asyncio.run(manager.get_model_for_task("answer")) == "local-chat:2b"
