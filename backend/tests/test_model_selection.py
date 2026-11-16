"""用户可选模型：目录过滤、请求校验、运行时回落。

这组测试关注三件容易做错的事：
1. 目录只给「可选 + 会聊天」的模型——embedding 模型混进对话选择器是最常见的错配
2. 不可选的模型必须 400，而不是被静默忽略（否则用户以为在用 A，其实一直用 B）
3. 选了但跑挂了要回落并留痕（`model_fallback`），不能静默替换
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.chat.common import MessageRequest, ensure_model_selectable
from src.services.model_manager import model_manager
from src.services.model_registry import ModelConfig, ProviderConfig, ResolvedModel, RouteConfig


def _provider() -> ProviderConfig:
    return ProviderConfig(id="p1", type="ollama", base_url="http://localhost:11434")


def _resolved(model_id: str, *, selectable: bool = True, caps=("chat", "stream")) -> ResolvedModel:
    return ResolvedModel(
        model=ModelConfig(
            id=model_id,
            provider="p1",
            model=f"m-{model_id}",
            display_name=f"显示-{model_id}",
            capabilities=list(caps),
            user_selectable=selectable,
        ),
        provider=_provider(),
        task="answer",
    )


class _Registry:
    def __init__(self, models, default=None):
        self._models = {m.model.id: m for m in models}
        self._default = default or (models[0] if models else None)
        self.config = SimpleNamespace(models=[m.model for m in models])
        self.source = "test"

    def get_model(self, model_id):
        if model_id not in self._models:
            raise KeyError(model_id)
        return self._models[model_id]

    def resolve(self, task, **kwargs):
        return SimpleNamespace(
            task=task,
            primary=self._default,
            fallbacks=[],
            config=RouteConfig(primary=self._default.model.id),
            source="test",
        )


@pytest.fixture
def registry(monkeypatch):
    """默认：a 可选、b 不可选、e 是 embedding。"""
    reg = _Registry(
        [
            _resolved("a"),
            _resolved("b", selectable=False),
            _resolved("e", selectable=True, caps=("embedding",)),
        ]
    )
    monkeypatch.setattr(model_manager, "_get_registry", lambda: reg)
    return reg


class TestResolveUserModel:
    def test_selectable_chat_model_ok(self, registry):
        assert model_manager.resolve_user_model("a").model.id == "a"

    def test_not_selectable_rejected(self, registry):
        """默认 false 的意义就在这里：没显式开放 = 不给用户选。"""
        assert model_manager.resolve_user_model("b") is None

    def test_embedding_model_rejected_even_if_selectable(self, registry):
        """标了可选但没有 chat 能力，也不该出现在对话里——这是标错配置时的兜底。"""
        assert model_manager.resolve_user_model("e") is None

    def test_unknown_and_empty_rejected(self, registry):
        assert model_manager.resolve_user_model("nope") is None
        assert model_manager.resolve_user_model("") is None


class TestEnsureSelectable:
    def test_ok_passes_through(self, registry):
        assert ensure_model_selectable("a") == "a"

    def test_empty_means_default(self, registry):
        assert ensure_model_selectable(None) is None

    def test_unselectable_raises_400(self, registry):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            ensure_model_selectable("b")
        assert exc.value.status_code == 400


class TestRequestValidation:
    def test_blank_becomes_none(self):
        assert MessageRequest(question="q", model_id="   ").model_id is None

    def test_too_long_rejected(self):
        with pytest.raises(Exception):
            MessageRequest(question="q", model_id="x" * 65)


class TestBuildGroup:
    def test_returns_none_when_not_selectable(self, registry):
        assert model_manager.build_user_model_group("b") is None

    def test_falls_back_to_default_route(self, registry):
        """选了 a，默认路由也是 a 时不应重复占两个 target。"""
        group = model_manager.build_user_model_group("a")
        assert group is not None
        assert len(group.targets) == 1

    def test_selected_model_first(self, registry, monkeypatch):
        """默认路由是 a、用户选 e2（可选 chat）时，a 应作为 fallback 排在后面。"""
        extra = _resolved("e2")
        registry._models["e2"] = extra
        registry.config.models.append(extra.model)

        group = model_manager.build_user_model_group("e2")

        ids = [t.model.id for t in group.targets]
        assert ids[0] == "e2"
        assert "a" in ids


class TestSelectLlmFallback:
    """运行时回落：选的时候可用、跑的时候不可用，要回落并留痕。"""

    def _chain(self):
        from src.services.pipeline.generation import GenerationMixin as Mixin

        class _Chain(Mixin):
            llm = "DEFAULT_THINKING"
            llm_direct = "DEFAULT_DIRECT"

        return _Chain()

    def test_override_used_and_marked(self, registry, monkeypatch):
        sentinel = object()
        monkeypatch.setattr(
            model_manager, "build_user_model_group", lambda *a, **k: sentinel
        )
        state = SimpleNamespace(model_id="a", resolved_model_id=None, model_fallback=False)

        llm = self._chain()._select_llm(state=state, think=False)

        assert llm is sentinel
        assert state.resolved_model_id == "a"
        assert state.model_fallback is False

    def test_unavailable_override_falls_back_and_marks(self, registry, monkeypatch):
        monkeypatch.setattr(model_manager, "build_user_model_group", lambda *a, **k: None)
        state = SimpleNamespace(model_id="b", resolved_model_id=None, model_fallback=False)

        llm = self._chain()._select_llm(state=state, think=False)

        assert llm == "DEFAULT_DIRECT", "回落要落到路由模型，而不是报错"
        assert state.model_fallback is True, "静默替换模型是最不该有的行为"

    def test_no_selection_keeps_route_behaviour(self, registry):
        state = SimpleNamespace(model_id=None, resolved_model_id=None, model_fallback=False)

        assert self._chain()._select_llm(state=state, think=False) == "DEFAULT_DIRECT"
        assert self._chain()._select_llm(state=state, think=None) == "DEFAULT_THINKING"


class TestCatalogEndpoint:
    def _client(self, registry):
        from src.api.model_catalog import router
        from src.auth import get_current_user

        app = FastAPI()
        app.include_router(router, prefix="/api")
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(user_id="u-1")
        return TestClient(app)

    def test_only_selectable_chat(self, registry):
        resp = self._client(registry).get("/api/models/available")

        assert resp.status_code == 200
        body = resp.json()
        assert [m["id"] for m in body["models"]] == ["a"]
        assert body["default_model_id"] == "a"
        assert body["models"][0]["is_default"] is True

    def test_does_not_leak_provider_details(self, registry):
        """目录是用户面：不能出现 provider_id / base_url / 单价。"""
        payload = self._client(registry).get("/api/models/available").json()

        dumped = str(payload)
        assert "provider" not in payload["models"][0]
        assert "localhost:11434" not in dumped
        assert "cost" not in dumped
