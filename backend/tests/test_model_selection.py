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


class TestServedModelReported:
    """「谁答的」要能被看见，而且要说得清是哪一份配置。

    ChatModelGroup 内部的 fallback 是静默的：主模型失败会自动试下一个，
    调用方无从得知换了模型。不记录这件事，「用户选了 A、A 挂了、实际是 B 答完」
    就永远不可见——而它正是最该告诉用户的那类事实。
    """

    @staticmethod
    def _chain():
        from src.services.pipeline.generation import GenerationMixin as Mixin

        class _Chain(Mixin):
            llm = "DEFAULT"
            llm_direct = "DEFAULT"

        return _Chain()

    @staticmethod
    def _fake_group(served_index, models):
        """models: [(id, upstream_name, display_name, provider_type), ...]"""
        from types import SimpleNamespace

        targets = [
            SimpleNamespace(
                model=SimpleNamespace(id=mid, model=name, display_name=display),
                provider=SimpleNamespace(type=ptype),
            )
            for mid, name, display, ptype in models
        ]
        return SimpleNamespace(last_served_index=served_index, targets=targets)

    @staticmethod
    def _state(model_id):
        from types import SimpleNamespace

        return SimpleNamespace(
            model_id=model_id,
            resolved_model_id=None,
            model_fallback=False,
            model_holder={},
        )

    def test_user_model_served_records_snapshot(self):
        state = self._state("chosen")
        group = self._fake_group(
            0,
            [
                ("chosen", "qwen3:8b", "Qwen3 8B", "openai_compatible"),
                ("default", "llama3:8b", "Llama 3 8B", "ollama"),
            ],
        )

        self._chain()._record_model_outcome(state, group)

        gen = state.model_holder["generation"]
        assert gen["model_id"] == "chosen"
        assert gen["model_name"] == "qwen3:8b", "要存上游名，配置改名后仍说得清当时跑的是什么"
        assert gen["display_name"] == "Qwen3 8B"
        assert gen["provider_type"] == "openai_compatible"
        assert gen["selected_by"] == "user"
        assert gen["fallback"] is False
        assert "fallback_from" not in gen, "没回落就不该出现回落来源"
        assert gen["schema_version"] == 1

    def test_group_fallback_records_from_which_model(self):
        state = self._state("chosen")
        group = self._fake_group(
            1,
            [
                ("chosen", "qwen3:8b", "Qwen3 8B", "openai_compatible"),
                ("default", "llama3:8b", "Llama 3 8B", "ollama"),
            ],
        )

        self._chain()._record_model_outcome(state, group)

        gen = state.model_holder["generation"]
        assert gen["model_id"] == "default"
        assert gen["fallback"] is True
        assert gen["fallback_from"] == "chosen"
        assert state.model_fallback is True

    def test_default_route_is_not_marked_as_fallback(self):
        """没选模型时本来就用默认，不能报「回落」——那是误报。"""
        state = self._state(None)
        group = self._fake_group(0, [("default", "llama3:8b", "Llama 3 8B", "ollama")])

        self._chain()._record_model_outcome(state, group)

        gen = state.model_holder["generation"]
        assert gen["selected_by"] == "default"
        assert gen["fallback"] is False
        assert state.model_fallback is False

    def test_missing_holder_does_not_raise(self):
        """非流式路径没有出口容器，记录逻辑必须容忍 None。"""
        state = self._state("chosen")
        state.model_holder = None
        group = self._fake_group(1, [("chosen", "a", "A", "ollama"), ("default", "b", "B", "ollama")])

        self._chain()._record_model_outcome(state, group)

        assert state.model_fallback is True
        assert state.resolved_model_id == "default"

    def test_no_served_target_records_nothing(self):
        """一次都没调成功（全挂）：不能编一个模型名出来，宁可不写。"""
        state = self._state("chosen")
        group = self._fake_group(None, [("chosen", "a", "A", "ollama")])

        self._chain()._record_model_outcome(state, group)

        assert "generation" not in state.model_holder


class TestCatalogDefaultModel:
    """「默认」要标在用户实际会用的模型上。

    `deep_thinking` 默认 off → 生成走 `answer_direct`；把它标成 `answer`（思考路径）
    不会报错，只会让人以为默认是思考模型。实测两个模型延迟差 3 倍以上，
    这种「标签错但功能正常」的缺陷只能靠测试钉住。
    """

    class _TaskRegistry:
        """按 task 返回不同主模型的注册表。"""

        def __init__(self, mapping):
            self._mapping = mapping
            self.config = SimpleNamespace(models=[m.model for m in mapping.values()])
            self.source = "test"

        def resolve(self, task, **kwargs):
            if task not in self._mapping:
                raise KeyError(task)
            return SimpleNamespace(primary=self._mapping[task], fallbacks=[], source="test")

        def get_model(self, model_id):
            for m in self._mapping.values():
                if m.model.id == model_id:
                    return m
            raise KeyError(model_id)

    def _client(self):
        from src.api.model_catalog import router
        from src.auth import get_current_user

        app = FastAPI()
        app.include_router(router, prefix="/api")
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(user_id="u-1")
        return TestClient(app)

    def test_prefers_answer_direct_over_answer(self, monkeypatch):
        reg = self._TaskRegistry({
            "answer": _resolved("thinking"),
            "answer_direct": _resolved("plain"),
        })
        monkeypatch.setattr(model_manager, "_get_registry", lambda: reg)

        body = self._client().get("/api/models/available").json()

        assert body["default_model_id"] == "plain", "默认应是 deep_thinking=off 实际走的那个"
        assert [m["id"] for m in body["models"] if m["is_default"]] == ["plain"]

    def test_falls_back_to_answer_when_no_direct_route(self, monkeypatch):
        """只有思考路由时（老配置）仍要能标出默认，不能空着。"""
        reg = self._TaskRegistry({"answer": _resolved("thinking")})
        monkeypatch.setattr(model_manager, "_get_registry", lambda: reg)

        body = self._client().get("/api/models/available").json()

        assert body["default_model_id"] == "thinking"


class TestReasoningCapability:
    """「这个模型能不能思考」是逐模型的事实，不是全局开关。

    同一 provider 下 qwen3 天生会思考、qwen2.5 完全不会。用全局的
    `OLLAMA_SUPPORTS_THINKING` 判断，等于告诉用户「这个模型能开深度思考」，
    打开后拿到的却是同一个更慢的回答——又一次「承诺了没做到」。
    """

    def test_reasoning_model_true(self, monkeypatch):
        reg = _Registry([_resolved("qwen3", caps=("chat", "stream", "reasoning"))])
        monkeypatch.setattr(model_manager, "_get_registry", lambda: reg)

        assert model_manager.model_supports_reasoning("qwen3") is True

    def test_plain_model_false(self, monkeypatch):
        reg = _Registry([_resolved("qwen25")])
        monkeypatch.setattr(model_manager, "_get_registry", lambda: reg)

        assert model_manager.model_supports_reasoning("qwen25") is False

    def test_unknown_model_returns_none(self, monkeypatch):
        """查不到就是不知道：返回 None 让调用方沿用默认，而不是断言「不能思考」。"""
        reg = _Registry([_resolved("qwen25")])
        monkeypatch.setattr(model_manager, "_get_registry", lambda: reg)

        assert model_manager.model_supports_reasoning("ghost") is None

    def test_empty_id_returns_none(self, monkeypatch):
        """未选模型（走默认路由）时不该在这里下结论。"""
        reg = _Registry([_resolved("qwen25")])
        monkeypatch.setattr(model_manager, "_get_registry", lambda: reg)

        assert model_manager.model_supports_reasoning("") is None
        assert model_manager.model_supports_reasoning(None) is None


class TestCatalogDefaultReasoning:
    """目录要告诉前端「默认模型能不能思考」。

    默认模型可能**没开放**给用户（不在 models 里），前端拿不到它的 capabilities，
    而「不选模型」时用的正是它。少了这个字段，深度思考开关的状态只能靠猜。
    """

    @staticmethod
    def _client():
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from src.api.model_catalog import router
        from src.auth import get_current_user

        app = FastAPI()
        app.include_router(router, prefix="/api")
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(user_id="u-1")
        return TestClient(app)

    @staticmethod
    def _reg(resolved):
        class _R:
            def __init__(self):
                self.config = SimpleNamespace(models=[resolved.model])
                self.source = "test"

            def resolve(self, task, **kwargs):
                return SimpleNamespace(primary=resolved, fallbacks=[], source="test")

        return _R()

    def test_true_when_default_has_reasoning(self, monkeypatch):
        resolved = _resolved("r1", caps=("chat", "reasoning"))
        monkeypatch.setattr(model_manager, "_get_registry", lambda: self._reg(resolved))

        body = self._client().get("/api/models/available").json()

        assert body["default_supports_reasoning"] is True

    def test_false_when_default_is_plain_chat(self, monkeypatch):
        resolved = _resolved("r1")
        monkeypatch.setattr(model_manager, "_get_registry", lambda: self._reg(resolved))

        body = self._client().get("/api/models/available").json()

        assert body["default_supports_reasoning"] is False
