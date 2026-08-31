"""
WebSocket 通知通道认证与权限测试（首帧鉴权协议）

协议：连接建立后客户端首帧发送 {"type": "auth", "api_key": "..."}，
服务端校验通过回发 {"type": "auth_ok"}，之后才开始业务消息；
失败或超时以 1008 关闭连接。
"""

import uuid

import pytest

# 依赖真实 PostgreSQL（TestClient 触发 app lifespan），默认跳过
pytestmark = pytest.mark.integration
from fastapi.testclient import TestClient

from src.config import settings
from src.main import app


def _auth_ok_frame(api_key: str) -> dict:
    """构造首帧鉴权消息。"""
    return {"type": "auth", "api_key": api_key}


@pytest.fixture
def client(integration_client):
    """委托进程级共享 TestClient（见 conftest.integration_client）。"""
    return integration_client


@pytest.fixture(autouse=True)
def reset_settings():
    """测试结束后恢复认证相关配置，避免影响其他测试。"""
    original_api_key = settings.security.API_KEY
    original_secret_key = settings.security.SECRET_KEY
    original_in_docker = settings.IN_DOCKER
    yield
    settings.security.API_KEY = original_api_key
    settings.security.SECRET_KEY = original_secret_key
    settings.IN_DOCKER = original_in_docker


class TestWebSocketAuth:
    """WebSocket 认证测试。"""

    def test_ws_notifications_anonymous_in_dev_allowed(self, client):
        """开发环境未配置 API_KEY 时，允许匿名 WebSocket 连接（服务端直接回 auth_ok）。"""
        settings.security.API_KEY = None
        settings.IN_DOCKER = False

        with client.websocket_connect("/api/ws/notifications") as websocket:
            data = websocket.receive_json()
            assert data == {"type": "auth_ok"}
            data = websocket.receive_json()
            assert data["type"] == "connected"

    def _configure_production_mode(self):
        """配置生产模式所需的强 SECRET_KEY、API_KEY 与 Docker 标志。"""
        settings.security.SECRET_KEY = "Test-Secret-Key-For-WebSocket-Auth-123!"
        settings.security.API_KEY = "test-api-key-for-websocket"
        settings.IN_DOCKER = True

    def test_ws_notifications_no_auth_in_docker_rejected(self, client):
        """Docker 生产环境连接后不发 auth 帧，应被关闭。"""
        self._configure_production_mode()

        with pytest.raises(Exception):
            with client.websocket_connect("/api/ws/notifications") as websocket:
                websocket.receive_json()

    def test_ws_notifications_valid_api_key_allowed(self, client):
        """首帧提供有效 API Key 时允许建立 WebSocket 连接。"""
        self._configure_production_mode()

        with client.websocket_connect("/api/ws/notifications") as websocket:
            websocket.send_json(_auth_ok_frame("test-api-key-for-websocket"))
            data = websocket.receive_json()
            assert data == {"type": "auth_ok"}
            data = websocket.receive_json()
            assert data["type"] == "connected"

    def test_ws_notifications_invalid_api_key_rejected(self, client):
        """首帧提供无效 API Key 时拒绝 WebSocket 连接。"""
        self._configure_production_mode()

        with pytest.raises(Exception):
            with client.websocket_connect("/api/ws/notifications") as websocket:
                websocket.send_json(_auth_ok_frame("wrong-key"))
                websocket.receive_json()

    def test_ws_notifications_non_json_first_frame_rejected(self, client):
        """首帧非 JSON 应被拒绝。"""
        self._configure_production_mode()

        with pytest.raises(Exception):
            with client.websocket_connect("/api/ws/notifications") as websocket:
                websocket.send_text("not-a-json")
                websocket.receive_json()

    def test_ws_kb_valid_api_key_allowed(self, client):
        """知识库通知通道首帧鉴权后可连接。"""
        self._configure_production_mode()

        with client.websocket_connect("/api/ws/kb") as websocket:
            websocket.send_json(_auth_ok_frame("test-api-key-for-websocket"))
            data = websocket.receive_json()
            assert data == {"type": "auth_ok"}
            data = websocket.receive_json()
            assert data["type"] == "connected"


class TestWebSocketDocsPermission:
    """文档通知通道权限测试。"""

    def _configure_production_mode(self):
        """配置生产模式所需的强 SECRET_KEY、API_KEY 与 Docker 标志。"""
        settings.security.SECRET_KEY = "Test-Secret-Key-For-WebSocket-Auth-123!"
        settings.security.API_KEY = "test-api-key-for-websocket"
        settings.IN_DOCKER = True

    def test_ws_docs_invalid_kb_id_rejected(self, client):
        """无效的知识库 ID 应被拒绝。"""
        self._configure_production_mode()

        with pytest.raises(Exception):
            with client.websocket_connect("/api/ws/docs/not-a-uuid") as websocket:
                websocket.send_json(_auth_ok_frame("test-api-key-for-websocket"))
                websocket.receive_json()

    def test_ws_docs_nonexistent_kb_rejected(self, client):
        """不存在的知识库 ID 应被拒绝。"""
        self._configure_production_mode()

        with pytest.raises(Exception):
            with client.websocket_connect(
                f"/api/ws/docs/{uuid.uuid4()}"
            ) as websocket:
                websocket.send_json(_auth_ok_frame("test-api-key-for-websocket"))
                websocket.receive_json()


class TestUploadProgressWSAuth:
    """上传进度 WebSocket 鉴权测试（回归：此前该端点完全无鉴权）。

    使用不触发 lifespan 的 TestClient：该 WS 端点仅依赖 settings，
    不需要 Redis/Milvus 预热，可在无外部服务的环境中运行。
    """

    @pytest.fixture
    def ws_client(self):
        return TestClient(app)

    def _configure_production_mode(self):
        """配置生产模式所需的强 SECRET_KEY、API_KEY 与 Docker 标志。"""
        settings.security.SECRET_KEY = "Test-Secret-Key-For-WebSocket-Auth-123!"
        settings.security.API_KEY = "test-api-key-for-websocket"
        settings.IN_DOCKER = True

    @pytest.fixture(autouse=True)
    def reset_settings(self):
        """测试结束后恢复认证相关配置。"""
        original_api_key = settings.security.API_KEY
        original_secret_key = settings.security.SECRET_KEY
        original_in_docker = settings.IN_DOCKER
        yield
        settings.security.API_KEY = original_api_key
        settings.security.SECRET_KEY = original_secret_key
        settings.IN_DOCKER = original_in_docker

    def test_upload_ws_anonymous_in_dev_allowed(self, ws_client):
        """开发环境未配置 API_KEY 时允许匿名连接。"""
        settings.security.API_KEY = None
        settings.IN_DOCKER = False

        with ws_client.websocket_connect(
            "/api/documents/upload/progress/ws/dev-upload-1"
        ) as websocket:
            assert websocket.receive_json() == {"type": "auth_ok"}
            websocket.send_json({"action": "ping"})
            assert websocket.receive_json() == {"type": "pong"}

    def test_upload_ws_no_auth_in_docker_rejected(self, ws_client):
        """Docker 生产模式不发 auth 帧应被关闭。"""
        self._configure_production_mode()

        with pytest.raises(Exception):
            with ws_client.websocket_connect(
                "/api/documents/upload/progress/ws/some-upload"
            ) as websocket:
                websocket.receive_json()

    def test_upload_ws_invalid_api_key_rejected(self, ws_client):
        """Docker 生产模式无效凭据必须被拒绝。"""
        self._configure_production_mode()

        with pytest.raises(Exception):
            with ws_client.websocket_connect(
                "/api/documents/upload/progress/ws/some-upload"
            ) as websocket:
                websocket.send_json(_auth_ok_frame("wrong-key"))
                websocket.receive_json()

    def test_upload_ws_valid_api_key_allowed(self, ws_client):
        """Docker 生产模式首帧有效凭据可建立连接并响应心跳。"""
        self._configure_production_mode()

        with ws_client.websocket_connect(
            "/api/documents/upload/progress/ws/some-upload"
        ) as websocket:
            websocket.send_json(_auth_ok_frame("test-api-key-for-websocket"))
            assert websocket.receive_json() == {"type": "auth_ok"}
            websocket.send_json({"action": "ping"})
            assert websocket.receive_json() == {"type": "pong"}
