"""端到端健康探针（P1-4）：/health/e2e 的分级与限流。

为什么单独一个文件：这个端点的价值不在「返回 200」，而在**失败时能说清卡在哪一段**。
只断言状态码的测试等于没测——ollama_unreachable / retrieval / generation / timeout
四种情况在运维上的处置完全不同。
"""

import pytest
from fastapi.testclient import TestClient

import src.main as main_module
from src.config import settings
from src.main import app

client = TestClient(app)

# 自动 fixture 会把探针换成「永远可达」，测探针本身时必须拿原始函数
_REAL_PROBE = main_module._probe_ollama_reachable


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

    async def _ollama_ok(timeout: float = 3.0) -> bool:
        return True

    monkeypatch.setattr(main_module, "_probe_ollama_reachable", _ollama_ok)
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

    def test_ollama_unreachable_is_its_own_stage(self, monkeypatch):
        """模型没了和代码坏了必须分开：前者重启后端无用。"""
        async def _ollama_down(timeout: float = 3.0) -> bool:
            return False

        monkeypatch.setattr(main_module, "_probe_ollama_reachable", _ollama_down)
        _patch_chain(monkeypatch, _FakeChain(("不该被调用", [], [], "llm_direct")))

        resp = client.get("/health/e2e")

        assert resp.status_code == 503
        assert resp.json()["stage"] == "ollama_unreachable"

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


class TestOllamaProbe:
    """探针本身也会假红：OLLAMA_HOST 允许 host / host:port / URL 三种写法。"""

    def _patch_http(self, monkeypatch, seen: dict):
        import httpx

        class _Resp:
            def raise_for_status(self):
                return None

        class _Client:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url):
                seen["url"] = url
                return _Resp()

        monkeypatch.setattr(httpx, "AsyncClient", _Client)

    def test_bare_host_gets_scheme_and_port(self, monkeypatch):
        """`host.docker.internal` 直拼会得到没有 scheme 的地址 → httpx 抛错 →
        探针 100% 报「Ollama 不可达」。2026-11-08 实测踩到，必须靠这里兜住。"""
        import asyncio

        seen: dict = {}
        self._patch_http(monkeypatch, seen)
        monkeypatch.setattr(settings.model, "OLLAMA_HOST", "host.docker.internal")

        assert asyncio.run(_REAL_PROBE()) is True
        assert seen["url"] == "http://host.docker.internal:11434/api/version"

    def test_full_url_kept_as_is(self, monkeypatch):
        import asyncio

        seen: dict = {}
        self._patch_http(monkeypatch, seen)
        monkeypatch.setattr(settings.model, "OLLAMA_HOST", "http://10.0.0.5:11435/")

        assert asyncio.run(_REAL_PROBE()) is True
        assert seen["url"] == "http://10.0.0.5:11435/api/version"
