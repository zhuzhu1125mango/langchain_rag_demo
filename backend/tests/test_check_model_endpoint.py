"""`scripts/check_model_endpoint.py` 的解析与判定。

这个脚本的价值是「把连不上拆成四类可分别处置的原因」，所以测试重点不在 HTTP，
而在两件事：能不能认出两种端点形状（OpenAI /v1/models 与 Ollama /api/tags），
以及维度不一致时**必须**给出明确结论——静默通过等于把它变成一个坑。
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_model_endpoint.py"
_spec = importlib.util.spec_from_file_location("check_model_endpoint", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
sys.modules["check_model_endpoint"] = mod
_spec.loader.exec_module(mod)


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _Client:
    def __init__(self, by_path):
        self.by_path = by_path
        self.calls = []

    async def get(self, url, headers=None):
        self.calls.append(url)
        for suffix, resp in self.by_path.items():
            if url.endswith(suffix):
                return resp
        return _Resp({}, 404)


def test_openai_shape():
    import asyncio

    client = _Client({"/models": _Resp({"data": [{"id": "qwen2.5-7b"}, {"id": "bge-m3"}]})})

    models = asyncio.run(mod.list_models(client, "http://x:1234/v1", {}))

    assert models == ["qwen2.5-7b", "bge-m3"]


def test_ollama_fallback_when_models_404():
    """Ollama 没有 /v1/models，要退到 /api/tags 而不是报「端点不可用」。"""
    import asyncio

    client = _Client({"/api/tags": _Resp({"models": [{"name": "bge-m3:latest"}]})})

    models = asyncio.run(mod.list_models(client, "http://x:11434/v1", {}))

    assert models == ["bge-m3:latest"]
    assert client.calls[0].endswith("/v1/models")
    assert client.calls[-1] == "http://x:11434/api/tags"


def test_unreachable_returns_2(monkeypatch, capsys):
    """连不上要和「连上了但没有模型」区分开：处置一个是网络，一个是配置。"""
    class _Boom:
        async def get(self, *a, **k):
            raise ConnectionError("Connection refused")

    class _ClientCtx:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return _Boom()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(mod.httpx, "AsyncClient", _ClientCtx)

    code = mod.main(["--base-url", "http://host.docker.internal:1234/v1"])

    assert code == 2
    out = capsys.readouterr().out
    assert "不可达" in out and "host.docker.internal" in out


def test_dimension_mismatch_fails(monkeypatch):
    """维度不一致必须判定失败——它不会报错，只会让检索静默返回空。"""

    async def _list(client, base, headers):
        return ["chat-model", "bge-small"]

    async def _chat(client, base, headers, model):
        return True, "ok"

    async def _embed(client, base, headers, model):
        return True, "ok", 768

    monkeypatch.setattr(mod, "list_models", _list)
    monkeypatch.setattr(mod, "try_chat", _chat)
    monkeypatch.setattr(mod, "try_embedding", _embed)

    code = mod.main(["--base-url", "http://x:1234/v1", "--expected-dim", "1024"])

    assert code == 1


def test_all_green_returns_0(monkeypatch):
    async def _list(client, base, headers):
        return ["chat-model", "bge-m3"]

    async def _chat(client, base, headers, model):
        return True, "ok"

    async def _embed(client, base, headers, model):
        return True, "ok", 1024

    monkeypatch.setattr(mod, "list_models", _list)
    monkeypatch.setattr(mod, "try_chat", _chat)
    monkeypatch.setattr(mod, "try_embedding", _embed)

    assert mod.main(["--base-url", "http://x:1234/v1"]) == 0
