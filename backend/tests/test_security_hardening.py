"""
安全加固批测试。

覆盖：API Key 常量时间比较、require_admin 鉴权规则、
session_id 解析 400、上传大小上限校验、进度终态清理。
"""

import asyncio
import datetime
import io
import uuid

import jwt as pyjwt
import pytest
from fastapi import HTTPException, UploadFile
from starlette.requests import Request as StarletteRequest

from src.auth import (
    _verify_api_key,
    require_admin,
    get_current_user,
    create_access_token,
    decode_access_token,
    refresh_access_token,
    CurrentUser,
)
from src.config import settings, CorsSettings
from src.api.chat import _parse_session_id
from src.api.document import _ensure_upload_size
from src.services import progress_manager
from src.utils.rate_limit import check_rate_limit, enforce_auth_rate_limit, reset_rate_limits


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


class TestAnonymousBypass:
    """开发模式匿名放行按 IS_PRODUCTION（APP_ENV）判定，非 Docker 裸跑生产不放行。"""

    async def test_bare_metal_prod_without_api_key_rejected(self):
        settings.security.API_KEY = None
        settings.APP_ENV = "production"
        settings.IN_DOCKER = False
        with pytest.raises(HTTPException) as exc:
            await get_current_user(api_key=None, jwt_credentials=None)
        assert exc.value.status_code == 401

    async def test_dev_without_api_key_anonymous_allowed(self):
        settings.security.API_KEY = None
        settings.APP_ENV = None
        settings.IN_DOCKER = False
        user = await get_current_user(api_key=None, jwt_credentials=None)
        assert user.user_id == "default"

    async def test_dev_container_anonymous_allowed(self):
        """APP_ENV=dev 的容器部署同为开发模式，匿名放行保持不变。"""
        settings.security.API_KEY = None
        settings.APP_ENV = "dev"
        settings.IN_DOCKER = True
        user = await get_current_user(api_key=None, jwt_credentials=None)
        assert user.user_id == "default"


class TestCorsDefaults:
    """CORS 默认值：credentials 开启时 methods/headers 不得为通配符。"""

    def test_no_wildcard_with_credentials(self):
        cors = CorsSettings()
        assert cors.ALLOW_CREDENTIALS is True
        assert "*" not in cors.ALLOW_METHODS
        assert "*" not in cors.ALLOW_HEADERS

    def test_auth_headers_allowed(self):
        cors = CorsSettings()
        assert "Authorization" in cors.ALLOW_HEADERS
        assert "X-API-Key" in cors.ALLOW_HEADERS
        assert "Content-Type" in cors.ALLOW_HEADERS


def _fake_request(ip: str = "1.2.3.4") -> StarletteRequest:
    """构造带 client 地址的最小 Request（限流键取 client.host）。"""
    return StarletteRequest({"type": "http", "client": (ip, 12345)})


class TestRateLimit:
    """进程内滑动窗口限流（W2-9）。"""

    def setup_method(self):
        reset_rate_limits()

    def teardown_method(self):
        reset_rate_limits()

    def test_within_limit_passes_then_blocks(self):
        for _ in range(3):
            assert check_rate_limit("t:ip1", 3) is True
        assert check_rate_limit("t:ip1", 3) is False

    def test_window_slide_restores_quota(self, monkeypatch):
        import src.utils.rate_limit as rl

        clock = [1000.0]
        monkeypatch.setattr(rl.time, "monotonic", lambda: clock[0])
        assert check_rate_limit("t:ip2", 1, window_seconds=60) is True
        assert check_rate_limit("t:ip2", 1, window_seconds=60) is False
        clock[0] += 61.0  # 窗口滑出
        assert check_rate_limit("t:ip2", 1, window_seconds=60) is True

    def test_keys_isolated(self):
        assert check_rate_limit("a:k", 1) is True
        assert check_rate_limit("b:k", 1) is True

    def test_enforce_throws_429_with_retry_after(self):
        for _ in range(2):
            check_rate_limit("enf:1.2.3.4", 2)
        with pytest.raises(HTTPException) as exc:
            enforce_auth_rate_limit(_fake_request("1.2.3.4"), "enf", 2)
        assert exc.value.status_code == 429
        assert exc.value.headers.get("Retry-After")

    def test_enforce_noop_when_limit_zero(self):
        enforce_auth_rate_limit(_fake_request(), "enf0", 0)  # 不抛即通过

    def test_enforce_uses_client_ip(self):
        enforce_auth_rate_limit(_fake_request("5.6.7.8"), "enf1", 1)
        # 同一 IP 第二次被拒；另一 IP 不受影响
        with pytest.raises(HTTPException):
            enforce_auth_rate_limit(_fake_request("5.6.7.8"), "enf1", 1)
        enforce_auth_rate_limit(_fake_request("9.9.9.9"), "enf1", 1)


_TEST_SECRET = "Unit-Test-Secret-Key-0123456789abcdef!"


class TestTokenRefresh:
    """JWT 滑动窗口刷新（W2-10）。"""

    @pytest.fixture(autouse=True)
    def _secret(self, monkeypatch):
        monkeypatch.setattr(settings.security, "SECRET_KEY", _TEST_SECRET)

    def test_access_token_ttl_reads_settings(self, monkeypatch):
        monkeypatch.setattr(settings.security, "ACCESS_TOKEN_EXPIRE_MINUTES", 60)
        payload = decode_access_token(create_access_token("user-1"))
        assert (payload["exp"] - payload["iat"]) == 60 * 60

    def test_refresh_roundtrip_preserves_orig_iat(self):
        old = decode_access_token(create_access_token("user-1"))
        new_token = refresh_access_token(create_access_token("user-1"))
        assert new_token is not None
        new = decode_access_token(new_token)
        assert new["sub"] == "user-1"
        assert new["orig_iat"] == old["orig_iat"]  # 链首签发时间保持
        assert new["exp"] >= old["exp"]  # 有效期后移（同秒签发时相等）

    def test_refresh_accepts_expired_token(self):
        """401 续期场景：原 token 已过 exp 但签名有效，仍可续期。"""
        now = datetime.datetime.now(datetime.timezone.utc)
        expired = pyjwt.encode(
            {
                "sub": "user-1",
                "iat": int((now - datetime.timedelta(hours=2)).timestamp()),
                "exp": int((now - datetime.timedelta(hours=1)).timestamp()),
                "orig_iat": int((now - datetime.timedelta(hours=2)).timestamp()),
            },
            _TEST_SECRET,
            algorithm="HS256",
        )
        assert refresh_access_token(expired) is not None

    def test_refresh_rejects_tampered_signature(self):
        token = create_access_token("user-1")
        tampered = token[:-6] + ("AAAAAA" if token[-6:] != "AAAAAA" else "BBBBBB")
        assert refresh_access_token(tampered) is None

    def test_refresh_rejects_orig_iat_beyond_window(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        token = create_access_token("user-1", orig_iat=now - datetime.timedelta(days=8))
        assert refresh_access_token(token) is None

    def test_refresh_disabled_when_max_age_zero(self, monkeypatch):
        monkeypatch.setattr(settings.security, "ACCESS_TOKEN_REFRESH_MAX_AGE_DAYS", 0)
        assert refresh_access_token(create_access_token("user-1")) is None

    def test_refresh_falls_back_to_iat_for_legacy_tokens(self):
        """旧格式 token 无 orig_iat：回退 iat 参与窗口校验（向后兼容）。"""
        now = datetime.datetime.now(datetime.timezone.utc)
        legacy_iat = int((now - datetime.timedelta(hours=1)).timestamp())
        legacy = pyjwt.encode(
            {
                "sub": "user-1",
                "iat": legacy_iat,
                "exp": int((now + datetime.timedelta(hours=1)).timestamp()),
            },
            _TEST_SECRET,
            algorithm="HS256",
        )
        new_token = refresh_access_token(legacy)
        assert new_token is not None
        assert decode_access_token(new_token)["orig_iat"] == legacy_iat


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
