"""P2 模型配置服务测试：Key 加密、安全响应、管理 API 鉴权。"""

import asyncio
import base64
import hashlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.admin_models import router as admin_models_router
from src.config import settings
from src.database import get_db
from src.models.model_registry import ModelDeployment, ModelProvider, ModelRoute
from src.services.model_config_service import (
    _provider_config,
    load_registry_config_from_db,
    safe_provider,
)
from src.services.model_secret_cipher import decrypt_secret, encrypt_secret, secret_hint


@pytest.fixture(autouse=True)
def _stable_encryption_key(monkeypatch):
    # 独立测试密钥，避免依赖 SECRET_KEY / 外部环境
    monkeypatch.setattr(
        settings.security,
        "MODEL_SECRET_ENC_KEY",
        base64.urlsafe_b64encode(hashlib.sha256(b"p2-test-key").digest()).decode(),
    )


def test_secret_encryption_roundtrip_and_hint():
    plaintext = "sk-test-1234567890"
    ciphertext = encrypt_secret(plaintext)

    assert ciphertext != plaintext
    assert decrypt_secret(ciphertext) == plaintext
    assert secret_hint(plaintext) == "****7890"


def test_provider_config_decrypts_but_safe_response_hides_key():
    row = ModelProvider(
        id="p1",
        name="openai",
        provider_type="openai_compatible",
        base_url="https://api.example.com/v1",
        api_key_ciphertext=encrypt_secret("sk-secret-1234"),
        api_key_env=None,
        api_key_required=True,
        headers={},
        timeout_s=30,
        external=True,
        enabled=True,
    )

    config = _provider_config(row)
    assert config.api_key == "sk-secret-1234"

    safe = asyncio.run(safe_provider(row))
    assert safe["has_api_key"] is True
    assert safe["api_key_hint"] == "****1234"
    assert "sk-secret-1234" not in str(safe)
    assert "api_key_ciphertext" not in safe


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, *row_groups):
        self._groups = list(row_groups)

    async def execute(self, *args, **kwargs):
        return _Rows(self._groups.pop(0))


def test_load_registry_config_from_db_rows():
    provider = ModelProvider(
        id="p1",
        name="openai",
        provider_type="openai_compatible",
        base_url="https://api.example.com/v1",
        api_key_ciphertext=encrypt_secret("sk-db-5678"),
        api_key_env=None,
        api_key_required=True,
        headers={},
        timeout_s=30,
        external=True,
        enabled=True,
    )
    model = ModelDeployment(
        id="m1",
        provider_id="p1",
        model_name="gpt-test",
        display_name="外部模型",
        capabilities=["chat", "stream", "tools"],
        context_window=8192,
        max_output_tokens=4096,
        embedding_dimension=0,
        enabled=True,
    )
    route = ModelRoute(
        id="r1",
        task="answer",
        primary_model_id="m1",
        fallback_model_ids=[],
        reasoning="native",
        allow_external_fallback=True,
    )

    config = asyncio.run(load_registry_config_from_db(_FakeDB([provider], [model], [route])))

    assert config is not None
    assert config.providers[0].api_key == "sk-db-5678"
    assert config.models[0].capabilities == ["chat", "stream", "tools"]
    assert config.routes["answer"].primary == "m1"
    assert config.routes["answer"].allow_external_fallback is True


def test_admin_model_api_requires_admin_key(monkeypatch):
    monkeypatch.setattr(settings.security, "ADMIN_KEY", "admin-test")

    class _EmptyResult:
        def scalars(self):
            return self

        def all(self):
            return []

    class _EmptyDB:
        async def execute(self, *args, **kwargs):
            return _EmptyResult()

    app = FastAPI()
    app.include_router(admin_models_router, prefix="/api")

    async def fake_db():
        yield _EmptyDB()

    app.dependency_overrides[get_db] = fake_db
    client = TestClient(app)

    blocked = client.get("/api/admin/models/providers")
    assert blocked.status_code in {401, 403}

    allowed = client.get("/api/admin/models/providers", headers={"X-Admin-Key": "admin-test"})
    assert allowed.status_code == 200
    assert allowed.json() == []
