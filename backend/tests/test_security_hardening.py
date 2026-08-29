"""
安全加固批测试。

覆盖：API Key 常量时间比较、require_admin 鉴权规则、
session_id 解析 400、上传大小上限校验、进度终态清理。
"""

import asyncio
import io
import uuid

import pytest
from fastapi import HTTPException, UploadFile

from src.auth import _verify_api_key, require_admin, CurrentUser
from src.config import settings
from src.api.chat import _parse_session_id
from src.api.document import _ensure_upload_size
from src.services import progress_manager


@pytest.fixture(autouse=True)
def restore_security_settings():
    """测试后恢复安全相关配置。"""
    saved = {
        "admin_key": settings.security.ADMIN_KEY,
        "api_key": settings.security.API_KEY,
        "app_env": settings.APP_ENV,
        "in_docker": settings.IN_DOCKER,
        "max_upload": settings.processing.MAX_UPLOAD_SIZE_MB,
    }
    yield
    settings.security.ADMIN_KEY = saved["admin_key"]
    settings.security.API_KEY = saved["api_key"]
    settings.APP_ENV = saved["app_env"]
    settings.IN_DOCKER = saved["in_docker"]
    settings.processing.MAX_UPLOAD_SIZE_MB = saved["max_upload"]


class TestVerifyApiKey:
    """API Key 校验改用 hmac.compare_digest。"""

    def test_correct_key_passes(self):
        settings.security.API_KEY = "correct-key-123"
        assert _verify_api_key("correct-key-123") is True

    def test_wrong_key_rejected(self):
        settings.security.API_KEY = "correct-key-123"
        assert _verify_api_key("wrong-key-456") is False

    def test_empty_key_rejected(self):
        settings.security.API_KEY = "correct-key-123"
        assert _verify_api_key("") is False
        assert _verify_api_key(None) is False

    def test_unconfigured_key_rejected(self):
        settings.security.API_KEY = None
        assert _verify_api_key("anything") is False


class TestRequireAdmin:
    """管理操作鉴权规则。"""

    async def test_admin_key_match_passes(self):
        settings.security.ADMIN_KEY = "admin-secret"
        user = CurrentUser(user_id="api_key_user", is_authenticated=True)
        assert (await require_admin(current_user=user, x_admin_key="admin-secret")) is user

    async def test_admin_key_mismatch_rejected(self):
        settings.security.ADMIN_KEY = "admin-secret"
        user = CurrentUser(user_id="api_key_user", is_authenticated=True)
        with pytest.raises(HTTPException) as exc:
            await require_admin(current_user=user, x_admin_key="wrong")
        assert exc.value.status_code == 403

    async def test_production_without_admin_key_rejected(self):
        settings.security.ADMIN_KEY = None
        settings.APP_ENV = "production"
        user = CurrentUser(user_id="api_key_user", is_authenticated=True)
        with pytest.raises(HTTPException) as exc:
            await require_admin(current_user=user, x_admin_key=None)
        assert exc.value.status_code == 403

    async def test_dev_without_admin_key_passes(self):
        settings.security.ADMIN_KEY = None
        settings.APP_ENV = None
        settings.IN_DOCKER = False
        user = CurrentUser(user_id="default", is_authenticated=True)
        assert (await require_admin(current_user=user, x_admin_key=None)) is user


class TestParseSessionId:
    """非法 session_id 返回 400 而非 500。"""

    def test_valid_uuid(self):
        sid = uuid.uuid4()
        assert _parse_session_id(str(sid)) == sid

    def test_none_passthrough(self):
        assert _parse_session_id(None) is None

    def test_empty_passthrough(self):
        assert _parse_session_id("") is None

    def test_invalid_returns_400(self):
        with pytest.raises(HTTPException) as exc:
            _parse_session_id("not-a-uuid; DROP TABLE users")
        assert exc.value.status_code == 400


def _make_upload_file(content: bytes) -> UploadFile:
    """构造带真实字节的 UploadFile（Starlette 会包装 file 对象）。"""
    return UploadFile(filename="test.txt", file=io.BytesIO(content))


class TestEnsureUploadSize:
    """上传大小按实测字节数校验。"""

    def test_within_limit_passes_and_seeks_back(self):
        settings.processing.MAX_UPLOAD_SIZE_MB = 1
        f = _make_upload_file(b"hello")
        assert _ensure_upload_size(f) == 5
        # 指针复位：后续上传逻辑可从头读取
        assert f.file.read() == b"hello"

    def test_oversized_returns_413(self):
        settings.processing.MAX_UPLOAD_SIZE_MB = 1
        f = _make_upload_file(b"x" * (1024 * 1024 + 1))
        with pytest.raises(HTTPException) as exc:
            _ensure_upload_size(f)
        assert exc.value.status_code == 413


class TestProgressTerminalCleanup:
    """进度记录终态自清理（内存泄漏修复）。"""

    def _fresh_store(self, upload_id):
        progress_manager.progress_store.pop(upload_id, None)
        progress_manager.ws_connections.pop(upload_id, None)

    async def test_terminal_status_scheduled_and_cleaned(self):
        upload_id = "sec-test-1"
        self._fresh_store(upload_id)
        try:
            progress_manager.create_upload_progress(upload_id, "a.txt", 100)
            progress_manager.update_upload_progress(upload_id, status="completed")
            # 直接调用清理协程（不等待真实 60s 调度）
            await progress_manager._cleanup_terminal_progress(upload_id)
            assert progress_manager.get_upload_progress(upload_id) is None
        finally:
            self._fresh_store(upload_id)

    async def test_non_terminal_not_cleaned(self):
        upload_id = "sec-test-2"
        self._fresh_store(upload_id)
        try:
            progress_manager.create_upload_progress(upload_id, "a.txt", 100)
            progress_manager.update_upload_progress(upload_id, status="uploading")
            await progress_manager._cleanup_terminal_progress(upload_id)
            assert progress_manager.get_upload_progress(upload_id) is not None
        finally:
            self._fresh_store(upload_id)

    async def test_terminal_cleanup_closes_ws_connections(self):
        upload_id = "sec-test-3"
        self._fresh_store(upload_id)
        try:
            progress_manager.create_upload_progress(upload_id, "a.txt", 100)
            progress_manager.update_upload_progress(upload_id, status="failed")

            closed = []

            class _FakeWS:
                def close(self):
                    closed.append(True)

            conn = _FakeWS()
            progress_manager.register_ws_connection(upload_id, conn)
            await progress_manager._cleanup_terminal_progress(upload_id)
            assert closed == [True]
            assert upload_id not in progress_manager.ws_connections
        finally:
            self._fresh_store(upload_id)
