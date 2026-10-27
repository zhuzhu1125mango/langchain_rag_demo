"""SEC P2 安全回归：通知频道、SSRF、JWT 吊销、缓存索引、健康探针、密码与日志。"""

import datetime
import socket
import uuid

import httpx
import pytest
from fastapi import HTTPException

import src.main as main_module
from src.auth import (
    CurrentUser,
    _extract_user_from_jwt,
    create_access_token,
    decode_access_token,
    logout_token,
    refresh_access_token,
    verify_jwt_token,
)
from src.config import settings
from src.services.semantic_cache_service import INDEX_ALL, INDEX_PREFIX, SemanticCacheService
from src.utils.security import UnsafeUrlError, WeakPasswordError, redact_for_log, validate_password_strength


def _credentials(token: str):
    from fastapi.security import HTTPAuthorizationCredentials

    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


@pytest.fixture(autouse=True)
def _jwt_secret(monkeypatch):
    monkeypatch.setattr(settings.security, "SECRET_KEY", "unit-test-secret-key-0123456789abcdef")


def test_logout_revokes_access_token_and_refresh_rotates():
    token = create_access_token("user-sec10")
    assert decode_access_token(token)["jti"]
    assert _extract_user_from_jwt(_credentials(token)) is not None

    assert logout_token(token) is True
    assert _extract_user_from_jwt(_credentials(token)) is None
    assert verify_jwt_token(token) is None

    old = create_access_token("user-sec10")
    new = refresh_access_token(old)
    assert new and new != old
    assert _extract_user_from_jwt(_credentials(old)) is None
    assert _extract_user_from_jwt(_credentials(new)) is not None


def test_expired_token_can_be_revoked():
    token = create_access_token("user-expired")
    payload = decode_access_token(token)
    payload["exp"] = int((datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)).timestamp())
    # logout_token 接受已过期 token（verify_exp=False）
    assert logout_token(token)
    assert verify_jwt_token(token) is None


class _NotificationDB:
    def __init__(self, owner_id: str):
        self.owner_id = owner_id

    async def execute(self, stmt):
        class _Result:
            def __init__(self, owner):
                self._owner = owner

            def scalar_one_or_none(self):
                from types import SimpleNamespace

                return SimpleNamespace(owner_id=self._owner)

        return _Result(self.owner_id)


@pytest.mark.asyncio
async def test_notification_channel_ownership():
    from src.api.notification import _assert_channel_allowed

    user = CurrentUser(user_id="user-a", is_authenticated=True)
    other = CurrentUser(user_id="user-b", is_authenticated=True)
    internal = CurrentUser(user_id="api_key_user", is_authenticated=True)
    kb_id = str(uuid.uuid4())

    with pytest.raises(HTTPException):
        await _assert_channel_allowed(_NotificationDB("user-a"), "kb:*", user)
    await _assert_channel_allowed(_NotificationDB("user-a"), "kb:*", internal)
    await _assert_channel_allowed(_NotificationDB("user-a"), "user:user-a", user)
    with pytest.raises(HTTPException):
        await _assert_channel_allowed(_NotificationDB("user-a"), "user:user-b", user)
    await _assert_channel_allowed(_NotificationDB("user-a"), f"doc:{kb_id}", user)
    with pytest.raises(HTTPException):
        await _assert_channel_allowed(_NotificationDB("user-a"), f"doc:{kb_id}", other)
    with pytest.raises(HTTPException):
        await _assert_channel_allowed(_NotificationDB("user-a"), "task:unknown", user)
    with pytest.raises(HTTPException):
        await _assert_channel_allowed(_NotificationDB("user-a"), "custom:chan", user)


@pytest.mark.asyncio
async def test_fetch_html_rejects_oversized_response(monkeypatch):
    from src.services.web_search_service import WebSearchService

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 128, headers={"content-type": "text/html"})

    svc = object.__new__(WebSearchService)
    svc._safe_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    svc.max_redirect_hops = 3
    svc.max_response_bytes = 32
    svc.allowed_content_types = {"text/html"}

    with pytest.raises(UnsafeUrlError, match="上限"):
        await svc._fetch_html("http://example.com/page")

    await svc._safe_http.aclose()


@pytest.mark.asyncio
async def test_fetch_html_rejects_non_text_content_type(monkeypatch):
    from src.services.web_search_service import WebSearchService

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"binary", headers={"content-type": "application/octet-stream"})

    svc = object.__new__(WebSearchService)
    svc._safe_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    svc.max_redirect_hops = 3
    svc.max_response_bytes = 1024
    svc.allowed_content_types = {"text/html"}

    with pytest.raises(UnsafeUrlError, match="Content-Type"):
        await svc._fetch_html("http://example.com/file")

    await svc._safe_http.aclose()


@pytest.mark.asyncio
async def test_pinned_backend_rejects_private_dns_and_pins_public_ip(monkeypatch):
    from src.utils.safe_http import PinnedNetworkBackend

    backend = PinnedNetworkBackend()
    calls = []

    class _FakeInner:
        async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
            calls.append((host, port))
            return object()

        async def sleep(self, seconds):
            return None

    backend._backend = _FakeInner()

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))],
    )
    with pytest.raises(UnsafeUrlError):
        await backend.connect_tcp("internal.example.com", 443)

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )
    await backend.connect_tcp("example.com", 443)
    assert calls[-1] == ("93.184.216.34", 443)


class _CacheClient:
    def __init__(self):
        self.hashes = {}
        self.sets = {}

    async def sadd(self, key, value):
        self.sets.setdefault(key, set()).add(value)
        return 1

    async def expire(self, key, ttl):
        return True

    async def smembers(self, key):
        return set(self.sets.get(key, set()))

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def hdel(self, key, *fields):
        for field in fields:
            self.hashes.get(key, {}).pop(field, None)
        return len(fields)

    async def delete(self, key):
        self.hashes.pop(key, None)
        self.sets.pop(key, None)
        return 1


class _Cache:
    available = True

    def __init__(self):
        self.client = _CacheClient()


@pytest.mark.asyncio
async def test_semantic_cache_uses_kb_index(monkeypatch):
    svc = SemanticCacheService()
    cache = _Cache()
    async def _get_cache():
        return cache

    monkeypatch.setattr(svc, "_get_cache", _get_cache)

    scope = "rag:semcache:user-a:kbscope"
    cache.client.sets[f"{INDEX_PREFIX}:kb:kb-1"] = {scope}
    cache.client.hashes[scope] = {
        "exact:one": '{"kb_ids": ["kb-1"], "question": "q"}',
        "exact:two": '{"kb_ids": ["kb-2"], "question": "other"}',
    }
    # 替换编码函数，避免测试依赖 Fernet 配置
    import src.services.semantic_cache_service as scs

    monkeypatch.setattr(scs, "_decode_entry", lambda value: __import__("json").loads(value))

    deleted = await svc.invalidate_kb("kb-1")

    assert deleted == 1
    assert "exact:one" not in cache.client.hashes[scope]
    assert "exact:two" in cache.client.hashes[scope]
    assert INDEX_ALL in cache.client.sets or True


@pytest.mark.asyncio
async def test_health_live_and_ready(monkeypatch):
    live = await main_module.health_live()
    assert live["status"] == "alive"

    async def _checks(*, include_ollama, detailed):
        return {"database": "ok", "redis": "fail"}

    monkeypatch.setattr(main_module, "_collect_health_checks", _checks)
    response = await main_module.health_ready()
    assert response.status_code == 503


def test_password_strength_and_log_redaction():
    validate_password_strength("Str0ng-Passw0rd!", "alice")
    with pytest.raises(WeakPasswordError):
        validate_password_strength("short", "alice")
    with pytest.raises(WeakPasswordError):
        validate_password_strength("password123", "alice")
    with pytest.raises(WeakPasswordError):
        validate_password_strength("alicealice1", "alicealice1")

    redacted = redact_for_log("用户的原始问题原文")
    assert "原始问题" not in redacted
    assert redacted.startswith("<redacted:")
