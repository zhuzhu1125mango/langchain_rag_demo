"""配置接口模型名暴露治理测试。

原始模型名会暴露底层模型/厂商，因此 /config 与 /config/model 必须仅管理员可读；
/config/info 与 /config/processing 继续对普通用户开放且不得包含模型名。
"""

from fastapi.testclient import TestClient

from src.config import settings
from src.main import app


def _client() -> TestClient:
    # 这些用例只走鉴权依赖，不触发 lifespan 外部服务；无需 with 上下文。
    return TestClient(app)


def test_model_config_requires_admin_key(monkeypatch):
    monkeypatch.setattr(settings.security, "ADMIN_KEY", "test-admin-key")
    client = _client()

    for path in ("/api/config/", "/api/config/model"):
        blocked = client.get(path)
        assert blocked.status_code in {401, 403}, path

    response = client.get(
        "/api/config/model",
        headers={"X-Admin-Key": "test-admin-key"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ollama_model_name"]
    assert body["ollama_direct_model_name"] is not None


def test_config_info_does_not_expose_model_names():
    response = _client().get("/api/config/info")

    assert response.status_code == 200
    text = response.text.lower()
    assert "ollama_model_name" not in text
    assert "qwen" not in text
    assert "features" in response.json()


def test_processing_config_stays_public():
    response = _client().get("/api/config/processing")

    assert response.status_code == 200
    assert set(response.json()) == {"chunk_size", "chunk_overlap", "top_k"}
