"""端到端健康探针（P1-4）：/health/e2e 的分级与限流。

为什么单独一个文件：这个端点的价值不在「返回 200」，而在**失败时能说清卡在哪一段**。
只断言状态码的测试等于没测——model_unreachable / retrieval / generation / timeout
四种情况在运维上的处置完全不同。
"""

import pytest
from fastapi.testclient import TestClient

import src.main as main_module
from src.config import settings
from src.main import app

client = TestClient(app)

# 自动 fixture 会把探针换成「永远可达」，测探针本身时必须拿原始函数
_REAL_PROBE = main_module._probe_active_model_providers


class _PymilvusError(Exception):
    """模拟 pymilvus 的连接类异常（模块前缀会被判为检索侧）。"""


_PymilvusError.__module__ = "pymilvus.exceptions"


class _FakeChain:
    def __init__(self, result=None, exc: BaseException | None = None):
        self._result = result
        self._exc = exc

    async def run(self, *args, **kwargs):
        if self._exc is not None:
            raise self._exc
        return self._result


def _patch_chain(monkeypatch, chain: _FakeChain) -> None:
    from src.services.rag_chain import RAGChain

    async def _get_instance(cls=None, **kwargs):
        return chain

    monkeypatch.setattr(RAGChain, "get_instance", classmethod(_get_instance))


@pytest.fixture(autouse=True)
def _reset_probe_state(monkeypatch):
    """限流状态与 Ollama 探测都得隔离：它们都是模块级/外部依赖。"""
    monkeypatch.setattr(main_module, "_e2e_last_run_at", 0.0)
    monkeypatch.setattr(main_module, "settings", settings)

    async def _model_ok(timeout: float = 2.0) -> tuple[bool, str]:
        return True, ""

    monkeypatch.setattr(main_module, "_probe_active_model_providers", _model_ok)
    yield


class TestHealthE2E:
    def test_ok_returns_latency_and_rid(self, monkeypatch):
        _patch_chain(monkeypatch, _FakeChain(("这是答案", [], [], "llm_direct")))

        resp = client.get("/health/e2e")

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["answer_type"] == "llm_direct"
        assert isinstance(data["latency_ms"], int) and data["latency_ms"] >= 0
        assert data["rid"], "没有 rid 就关联不到日志"

    def test_model_unreachable_is_its_own_stage(self, monkeypatch):
        """模型没了和代码坏了必须分开：前者重启后端无用。

        断言 stage 与 detail 都要有：detail 里带着 provider 的 type@base_url，
        运维才能一眼看出该去看 Ollama 还是 LM Studio。
        """
        async def _model_down(timeout: float = 2.0) -> tuple[bool, str]:
            return False, "openai_compatible@http://host.docker.internal:1234/v1: connect failed"

        monkeypatch.setattr(main_module, "_probe_active_model_providers", _model_down)
        _patch_chain(monkeypatch, _FakeChain(("不该被调用", [], [], "llm_direct")))

        resp = client.get("/health/e2e")

        assert resp.status_code == 503
        assert resp.json()["stage"] == "model_unreachable"
        assert "1234" in resp.json()["error"], "失败信息要带上到底哪个 provider 连不上"

    def test_empty_answer_is_failure_not_success(self, monkeypatch):
        """链路都通但答案为空，比抛异常更隐蔽——用户拿到的是空白。"""
        _patch_chain(monkeypatch, _FakeChain(("   ", [], [], "knowledge_base")))

        resp = client.get("/health/e2e")

        assert resp.status_code == 503
        assert resp.json()["stage"] == "generation"

    def test_retrieval_exception_maps_to_retrieval_stage(self, monkeypatch):
        _patch_chain(monkeypatch, _FakeChain(exc=_PymilvusError("fail to connect milvus")))

        resp = client.get("/health/e2e")

        assert resp.status_code == 503
        data = resp.json()
        assert data["stage"] == "retrieval"
        assert "pymilvus" in data["error"].lower()

    def test_generation_exception_maps_to_generation_stage(self, monkeypatch):
        _patch_chain(monkeypatch, _FakeChain(exc=RuntimeError("prompt template broken")))

        resp = client.get("/health/e2e")

        assert resp.status_code == 503
        assert resp.json()["stage"] == "generation"

    def test_timeout_stage(self, monkeypatch):
        import asyncio as _asyncio

        _patch_chain(monkeypatch, _FakeChain(exc=_asyncio.TimeoutError()))

        resp = client.get("/health/e2e")

        assert resp.status_code == 503
        assert resp.json()["stage"] == "timeout"

    def test_rate_limited_to_one_per_interval(self, monkeypatch):
        """这个端点会真的花一次 LLM 调用，不能让人当压测接口刷。"""
        monkeypatch.setattr(settings.health_probe, "HEALTH_E2E_MIN_INTERVAL_SECONDS", 600)
        _patch_chain(monkeypatch, _FakeChain(("答案", [], [], "llm_direct")))

        assert client.get("/health/e2e").status_code == 200
        second = client.get("/health/e2e")

        assert second.status_code == 429
        assert "Retry-After" in second.headers

    def test_disabled_returns_404(self, monkeypatch):
        monkeypatch.setattr(settings.health_probe, "HEALTH_E2E_ENABLED", False)
        _patch_chain(monkeypatch, _FakeChain(("答案", [], [], "llm_direct")))

        assert client.get("/health/e2e").status_code == 404

    def test_requires_admin(self, monkeypatch):
        """开发模式默认放行；显式开启鉴权后匿名调用必须被挡。"""
        monkeypatch.setattr(settings, "IN_DOCKER", True)
        monkeypatch.setattr(settings.security, "API_KEY", "test-key")
        _patch_chain(monkeypatch, _FakeChain(("答案", [], [], "llm_direct")))

        resp = client.get("/health/e2e")

        assert resp.status_code in (401, 403)


class TestActiveProviderProbe:
    """探针必须探「当前路由指向的 provider」，而不是写死 Ollama。

    2026-11-15 的教训：宿主把 Ollama 换成 LM Studio 后，固定打 `/api/version`
    的实现会永远报红——而模型服务明明好着。探针探错对象比不探更糟，
    它会训练人忽略红灯。
    """

    @staticmethod
    def _registry(mapping: dict):
        from types import SimpleNamespace

        class _Registry:
            def resolve(self, task):
                if task not in mapping:
                    raise KeyError(task)
                provider = SimpleNamespace(type=mapping[task][0], base_url=mapping[task][1])
                return SimpleNamespace(primary=SimpleNamespace(provider=provider))

        return _Registry()

    def test_same_provider_probed_once(self, monkeypatch):
        """answer / answer_direct / fast 常指向同一 provider，探三遍是浪费。"""
        import asyncio

        from src.services import model_config_service as mcs
        from src.services.model_manager import model_manager

        calls: list = []

        async def _fake_test(provider, timeout=12.0):
            calls.append((provider.type, provider.base_url))
            return {"ok": True, "models": []}

        url = "http://host.docker.internal:1234/v1"
        monkeypatch.setattr(model_manager, "_get_registry",
                            lambda: self._registry({"answer": ("openai_compatible", url),
                                                    "fast": ("openai_compatible", url)}))
        monkeypatch.setattr(mcs, "test_provider", _fake_test)

        ok, detail = asyncio.run(_REAL_PROBE())

        assert ok is True and detail == ""
        assert calls == [("openai_compatible", url)], "同一 provider 应该只探一次"

    def test_failure_detail_names_the_provider(self, monkeypatch):
        """只回「模型不可达」等于没回——要能看出该去看哪个进程。"""
        import asyncio

        from src.services import model_config_service as mcs
        from src.services.model_manager import model_manager

        async def _fake_test(provider, timeout=12.0):
            return {"ok": False, "error": "connect failed"}

        monkeypatch.setattr(model_manager, "_get_registry",
                            lambda: self._registry({"answer": ("openai_compatible",
                                                               "http://host.docker.internal:1234/v1")}))
        monkeypatch.setattr(mcs, "test_provider", _fake_test)

        ok, detail = asyncio.run(_REAL_PROBE())

        assert ok is False
        assert "openai_compatible@" in detail and "1234" in detail

    def test_no_route_is_not_healthy(self, monkeypatch):
        """一条路由都没有时空注册表不能算「健康」——那是没配，不是没事。"""
        import asyncio

        from src.services.model_manager import model_manager

        monkeypatch.setattr(model_manager, "_get_registry", lambda: self._registry({}))

        ok, detail = asyncio.run(_REAL_PROBE())

        assert ok is False
        assert "没有可用的模型路由" in detail
