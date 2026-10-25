"""用户级 API Key 单元测试（E.6，独立子应用 + 依赖覆盖，无外部服务）。

覆盖：
- 端点级：开关关闭 404 / 发放一次性明文（DB 仅存哈希）/ 上限 422 /
  列表永不返回明文 / 吊销 204 + 越权 403 + 重复吊销 409 /
  匿名身份 400 / 发放限流 429
- 认证链级：_resolve_api_key 开关/命中/未命中/吊销/过期/last_used_at 节流
- HTTP 集成：X-API-Key 携带用户级 Key 经 get_current_user 真实认证链命中
- WS 首帧：api_key 字段走用户级 Key 认证通过
- /config/info：user_api_keys_enabled 字段随开关取值
"""

import asyncio
import hashlib
import itertools
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.auth import router
from src.auth import CurrentUser, get_current_user, get_current_user_for_ws
from src.config import settings
from src.database import get_db
from src.utils.rate_limit import reset_rate_limits

USER_ID = str(uuid.uuid4())
OTHER_ID = str(uuid.uuid4())
_RATE_LIMIT_IP_COUNTER = itertools.count(10)


class FakeScalars:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items


class FakeResult:
    def __init__(self, items):
        self._scalars = FakeScalars(items)

    def scalars(self):
        return self._scalars


class FakeDB:
    """按调用序弹出 scalar 结果、记录 add 的假会话。"""

    def __init__(self):
        self.scalar_queue = []
        self.scalars_all_queue = []
        self.added = []
        self.commit_count = 0

    async def scalar(self, stmt):
        return self.scalar_queue.pop(0) if self.scalar_queue else None

    async def execute(self, stmt):
        items = self.scalars_all_queue.pop(0) if self.scalars_all_queue else []
        return FakeResult(items)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commit_count += 1

    async def refresh(self, obj):
        obj.created_at = datetime.now(timezone.utc)

    async def rollback(self):
        pass


def make_key_row(key_id: str, owner_id: str, revoked: bool = False, expires_in_days: int = None):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid.UUID(key_id),
        user_id=uuid.UUID(owner_id),
        name="claude-code",
        key_hash="h" * 64,
        key_prefix="rag_a1b2c3d4",
        created_at=now,
        expires_at=now + timedelta(days=expires_in_days) if expires_in_days else None,
        last_used_at=None,
        revoked_at=now if revoked else None,
    )


class FakeKeySession:
    """_resolve_api_key 专用假短会话（async context manager）。"""

    def __init__(self, row):
        self.row = row
        self.commit_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def scalar(self, stmt):
        return self.row

    async def commit(self):
        self.commit_count += 1

    async def rollback(self):
        pass


class FakeKeyRow:
    """_resolve_api_key 结果行（含 is_valid 与 last_used_at 行为）。"""

    def __init__(self, user_id: str, valid: bool = True, last_used: datetime = None):
        self.user_id = uuid.UUID(user_id)
        self.last_used_at = last_used
        self._valid = valid

    def is_valid(self, now: datetime) -> bool:
        return self._valid


class FakeWebSocket:
    """get_current_user_for_ws 直测用假连接。"""

    def __init__(self, first_frame):
        self._first_frame = first_frame
        self.sent = []
        self.closed_code = None

    async def accept(self):
        pass

    async def send_json(self, data):
        self.sent.append(data)

    async def close(self, code=None, reason=None):
        self.closed_code = code

    async def receive_json(self):
        return self._first_frame


async def _noop_audit(**kwargs):
    return None


@pytest.fixture
def client(monkeypatch):
    """独立子应用：仅挂 auth 路由，覆盖 get_db / get_current_user。

    开关默认开启；审计 mock 为 no-op（record_audit 内部自开会话会连真实 DB）。
    """
    app = FastAPI()
    app.include_router(router, prefix="/api")

    db = FakeDB()
    user = CurrentUser(user_id=USER_ID, is_authenticated=True)

    async def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: user

    monkeypatch.setattr(settings.security, "USER_API_KEYS_ENABLED", True)
    monkeypatch.setattr("src.services.audit_service.record_audit", _noop_audit)
    # 每个用例独立客户端 IP：Redis 限流 key 不跨用例累计（CI 与本地一致）
    ip = f"198.51.100.{next(_RATE_LIMIT_IP_COUNTER)}"
    monkeypatch.setattr("src.utils.rate_limit.get_client_ip", lambda request: ip)
    # 端点单测固定走进程内桶：避免复用集成测试的 Redis 单例（可能绑定已关闭的事件循环）
    monkeypatch.setattr("src.utils.rate_limit._redis_client", lambda: None)
    reset_rate_limits()
    return TestClient(app), db, user


KEY_ID = str(uuid.uuid4())


class TestEndpointGuards:
    def test_disabled_returns_404(self, client, monkeypatch):
        http, _, _ = client
        monkeypatch.setattr(settings.security, "USER_API_KEYS_ENABLED", False)
        assert http.post("/api/auth/api-keys", json={"name": "x"}).status_code == 404
        assert http.get("/api/auth/api-keys").status_code == 404
        assert http.delete(f"/api/auth/api-keys/{KEY_ID}").status_code == 404

    def test_anonymous_owner_rejected(self, client):
        """default/api_key_user 等非注册身份发放被拒（防止万能 Key）。"""
        http, _, _ = client
        http.app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id="default", is_authenticated=True
        )
        resp = http.post("/api/auth/api-keys", json={"name": "x"})
        assert resp.status_code == 400


class TestCreateApiKey:
    def test_create_returns_plaintext_once_and_stores_hash(self, client):
        """明文仅创建响应返回一次；DB 行仅存 SHA-256（不含明文）。"""
        http, db, _ = client
        resp = http.post("/api/auth/api-keys", json={"name": "claude-code-office"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["key"].startswith("rag_") and len(body["key"]) >= 40
        assert body["key_prefix"] == body["key"][:12]
        row = db.added[0]
        assert row.key_hash == hashlib.sha256(body["key"].encode("utf-8")).hexdigest()
        assert row.key_hash not in body["key"]
        assert row.expires_at is None

    def test_create_with_expiry(self, client):
        http, db, _ = client
        resp = http.post("/api/auth/api-keys", json={"name": "ci", "expires_in_days": 30})
        assert resp.status_code == 201
        assert db.added[0].expires_at is not None

    def test_create_rejected_over_limit(self, client, monkeypatch):
        http, db, _ = client
        monkeypatch.setattr(settings.security, "USER_API_KEYS_MAX_PER_USER", 5)
        db.scalar_queue.append(5)  # 活跃 Key 数已达上限
        resp = http.post("/api/auth/api-keys", json={"name": "x"})
        assert resp.status_code == 422

    def test_create_rate_limited(self, client, monkeypatch):
        http, _, _ = client
        monkeypatch.setattr(settings.security, "AUTH_RATE_LIMIT_API_KEY_CREATE", 1)
        # 第一发 201（FakeDB.scalar 默认 None → 不超限）
        assert http.post("/api/auth/api-keys", json={"name": "x"}).status_code == 201
        # 第二发命中限流 429
        assert http.post("/api/auth/api-keys", json={"name": "y"}).status_code == 429


class TestListApiKeys:
    def test_list_never_returns_plaintext(self, client):
        http, db, _ = client
        db.scalars_all_queue.append(
            [make_key_row(KEY_ID, USER_ID), make_key_row(str(uuid.uuid4()), USER_ID, revoked=True)]
        )
        resp = http.get("/api/auth/api-keys")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 2
        for item in items:
            assert "key" not in item
            assert item["key_prefix"].startswith("rag_")
            assert set(item) == {
                "id", "name", "key_prefix", "created_at",
                "last_used_at", "expires_at", "revoked_at",
            }

    def test_list_empty_for_anonymous_identity(self, client):
        http, _, _ = client
        http.app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id="api_key_user", is_authenticated=True
        )
        assert http.get("/api/auth/api-keys").json() == []


class TestRevokeApiKey:
    def test_revoke_success_204(self, client):
        http, db, _ = client
        row = make_key_row(KEY_ID, USER_ID)
        db.scalar_queue.append(row)
        resp = http.delete(f"/api/auth/api-keys/{KEY_ID}")
        assert resp.status_code == 204
        assert row.revoked_at is not None

    def test_revoke_other_users_key_403(self, client):
        http, db, _ = client
        db.scalar_queue.append(make_key_row(KEY_ID, OTHER_ID))
        assert http.delete(f"/api/auth/api-keys/{KEY_ID}").status_code == 403

    def test_revoke_already_revoked_409(self, client):
        http, db, _ = client
        db.scalar_queue.append(make_key_row(KEY_ID, USER_ID, revoked=True))
        assert http.delete(f"/api/auth/api-keys/{KEY_ID}").status_code == 409

    def test_revoke_unknown_404(self, client):
        http, db, _ = client
        db.scalar_queue.append(None)
        assert http.delete(f"/api/auth/api-keys/{KEY_ID}").status_code == 404


class TestResolveApiKey:
    def _patch_session(self, monkeypatch, row):
        holder = FakeKeySession(row)
        monkeypatch.setattr("src.database.async_session_maker", lambda: holder)
        return holder

    def test_disabled_returns_none_without_query(self, monkeypatch):
        from src.auth import _resolve_api_key

        monkeypatch.setattr(settings.security, "USER_API_KEYS_ENABLED", False)

        def _boom():
            raise AssertionError("开关关闭时不应触达数据库会话")

        # 会话工厂被触达即失败
        monkeypatch.setattr("src.database.async_session_maker", _boom)
        assert asyncio.run(_resolve_api_key("rag_any")) is None

    def test_valid_key_resolves_to_owner(self, monkeypatch):
        from src.auth import _resolve_api_key

        monkeypatch.setattr(settings.security, "USER_API_KEYS_ENABLED", True)
        holder = self._patch_session(monkeypatch, FakeKeyRow(USER_ID, valid=True))
        user = asyncio.run(_resolve_api_key("rag_valid"))
        assert user is not None and user.user_id == USER_ID
        # 首次使用（last_used_at=None）应更新并落库
        assert holder.commit_count == 1
        assert holder.row.last_used_at is not None

    def test_invalid_or_missing_key_returns_none(self, monkeypatch):
        from src.auth import _resolve_api_key

        monkeypatch.setattr(settings.security, "USER_API_KEYS_ENABLED", True)
        self._patch_session(monkeypatch, None)
        assert asyncio.run(_resolve_api_key("rag_missing")) is None
        self._patch_session(monkeypatch, FakeKeyRow(USER_ID, valid=False))
        assert asyncio.run(_resolve_api_key("rag_revoked")) is None

    def test_last_used_throttled_within_window(self, monkeypatch):
        """距上次记录 <60s 不重复写库。"""
        from src.auth import _resolve_api_key

        monkeypatch.setattr(settings.security, "USER_API_KEYS_ENABLED", True)
        recent = datetime.now(timezone.utc) - timedelta(seconds=10)
        holder = self._patch_session(monkeypatch, FakeKeyRow(USER_ID, valid=True, last_used=recent))
        assert asyncio.run(_resolve_api_key("rag_valid")) is not None
        assert holder.commit_count == 0


class TestHttpAuthChain:
    def test_x_api_key_authenticates_as_bound_user(self, monkeypatch):
        """X-API-Key 携带用户级 Key 经真实 get_current_user 命中绑定用户。"""
        app = FastAPI()
        app.include_router(router, prefix="/api")
        db = FakeDB()

        async def override_db():
            yield db

        app.dependency_overrides[get_db] = override_db  # /me 的会话（查 users 表）

        monkeypatch.setattr(settings.security, "USER_API_KEYS_ENABLED", True)
        monkeypatch.setattr(settings.security, "API_KEY", None)  # 确保不走全局 Key 分支
        plain = "rag_testkey_abcdefgh12345678"
        key_hash = hashlib.sha256(plain.encode("utf-8")).hexdigest()

        seen_hash = {}

        class CapturingSession(FakeKeySession):
            async def scalar(self, stmt):
                seen_hash["value"] = True
                return FakeKeyRow(USER_ID, valid=True)

        holder = CapturingSession(None)
        monkeypatch.setattr("src.database.async_session_maker", lambda: holder)

        with TestClient(app) as http:
            resp = http.get("/api/auth/me", headers={"X-API-Key": plain})
        assert resp.status_code == 200
        assert resp.json()["user_id"] == USER_ID
        assert seen_hash  # 认证链确实走了 per-user 查询

    def test_ws_first_frame_with_user_key(self, monkeypatch):
        """WS 首帧 api_key 字段走用户级 Key 认证通过。

        需配置全局 API_KEY（非空）：开发模式（无全局 Key 且非生产）下 WS
        匿名放行分支在最前，不触达 Key 校验——这是既有设计。
        """
        monkeypatch.setattr(settings.security, "USER_API_KEYS_ENABLED", True)
        monkeypatch.setattr(settings.security, "API_KEY", "global-key-placeholder")
        holder = FakeKeySession(FakeKeyRow(USER_ID, valid=True))
        monkeypatch.setattr("src.database.async_session_maker", lambda: holder)

        ws = FakeWebSocket({"type": "auth", "api_key": "rag_ws_key_1234567890abcdef"})  # gitleaks:allow (测试夹具假值)
        user = asyncio.run(get_current_user_for_ws(ws))
        assert user.user_id == USER_ID
        assert ws.sent == [{"type": "auth_ok"}]
        assert ws.closed_code is None


class TestConfigInfo:
    def test_user_api_keys_enabled_flag(self, monkeypatch):
        from src.api.config import get_system_info

        monkeypatch.setattr(settings.security, "USER_API_KEYS_ENABLED", True)
        assert asyncio.run(get_system_info())["user_api_keys_enabled"] is True
        monkeypatch.setattr(settings.security, "USER_API_KEYS_ENABLED", False)
        assert asyncio.run(get_system_info())["user_api_keys_enabled"] is False
