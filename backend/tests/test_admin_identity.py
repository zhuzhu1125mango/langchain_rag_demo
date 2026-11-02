"""P0-2 管理员身份：从全局 ADMIN_KEY 迁到「用户表上的 is_admin」。

覆盖四件事：
1. `_is_admin_user` 的判定与 fail-closed（DB 故障不放行、非 UUID 身份一律非管理员）
2. `require_admin` 的三条通道优先级（用户身份 > 应急 Key > 开发模式）
3. 首个注册用户自举为管理员（空库能建号），存量库不会意外提权
4. `scripts/create_admin.py` 的新建 / 提升 / 幂等 / 弱密码拒绝
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import src.auth as auth_module
from src.api.auth import RegisterRequest, register
from src.auth import CurrentUser, _is_admin_user, require_admin
from src.config import settings
from src.models import User


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
class _SessionCM:
    """async with async_session_maker() as db 的最小替身。"""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, scalar_value=None, execute_rows=()):
        self._scalar = scalar_value
        self._rows = list(execute_rows)
        self.added = []
        self.commits = 0

    async def scalar(self, stmt):
        return self._scalar

    async def execute(self, stmt):
        return _Result(self._rows)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        return None


def _patch_session(monkeypatch, session):
    """_is_admin_user 内部 `from src.database import async_session_maker`（调用时解析），
    因此必须打在 src.database 上，打在 src.auth 上无效。"""
    monkeypatch.setattr(
        "src.database.async_session_maker", lambda: _SessionCM(session), raising=True
    )


def _user(uid: str | None = None, admin: bool = False, name: str = "alice") -> User:
    return User(
        id=uuid.UUID(uid) if uid else uuid.uuid4(),
        username=name,
        password_hash="x" * 60,
        is_admin=admin,
    )


@pytest.fixture(autouse=True)
def _restore_admin_key():
    saved = settings.security.ADMIN_KEY
    yield
    settings.security.ADMIN_KEY = saved


# ---------------------------------------------------------------------------
# 1. _is_admin_user
# ---------------------------------------------------------------------------
class TestIsAdminUser:
    async def test_admin_flag_true(self, monkeypatch):
        target = _user(admin=True)
        _patch_session(monkeypatch, _FakeSession(scalar_value=True))
        assert await _is_admin_user(CurrentUser(user_id=str(target.id), is_authenticated=True))

    async def test_non_admin_flag_false(self, monkeypatch):
        target = _user(admin=False)
        _patch_session(monkeypatch, _FakeSession(scalar_value=False))
        assert not await _is_admin_user(
            CurrentUser(user_id=str(target.id), is_authenticated=True)
        )

    async def test_non_uuid_identity_skips_query(self, monkeypatch):
        """开发默认用户 / 全局 API Key 身份没有用户记录，不该白查一次库。"""

        def _boom():
            raise AssertionError("非 UUID 身份不应触发数据库查询")

        monkeypatch.setattr("src.database.async_session_maker", _boom, raising=True)
        for ident in ("default", "api_key_user", ""):
            assert not await _is_admin_user(
                CurrentUser(user_id=ident, is_authenticated=True)
            )

    async def test_db_failure_is_denied(self, monkeypatch):
        """授权判断必须 fail-closed：查不到就拒绝，且要上报（不能悄悄降级）。"""
        logged = []

        class _BrokenSession:
            async def scalar(self, stmt):
                raise RuntimeError("connection refused")

        _patch_session(monkeypatch, _BrokenSession())
        monkeypatch.setattr(
            auth_module,
            "log_exception",
            lambda *a, **kw: logged.append(kw),
        )

        user = CurrentUser(user_id=str(uuid.uuid4()), is_authenticated=True)
        assert await _is_admin_user(user) is False
        assert logged and logged[0]["action"] == "auth.admin_lookup"


# ---------------------------------------------------------------------------
# 2. require_admin 三条通道
# ---------------------------------------------------------------------------
class TestRequireAdmin:
    async def test_user_identity_passes_even_when_admin_key_configured(self, monkeypatch):
        """核心改动：配了 ADMIN_KEY 也不再是唯一通道，管理员账号可直接进。"""
        settings.security.ADMIN_KEY = "global-key"

        async def _is_admin(_user):
            return True

        monkeypatch.setattr(auth_module, "_is_admin_user", _is_admin)
        user = CurrentUser(user_id="u1", is_authenticated=True)

        assert await require_admin(current_user=user, x_admin_key=None) is user

    async def test_non_admin_without_key_is_403(self, monkeypatch):
        settings.security.ADMIN_KEY = "global-key"

        async def _not_admin(_user):
            return False

        monkeypatch.setattr(auth_module, "_is_admin_user", _not_admin)
        user = CurrentUser(user_id="u1", is_authenticated=True)

        with pytest.raises(HTTPException) as exc:
            await require_admin(current_user=user, x_admin_key=None)
        assert exc.value.status_code == 403

    async def test_bootstrap_key_still_works_and_warns(self, monkeypatch):
        settings.security.ADMIN_KEY = "global-key"

        async def _not_admin(_user):
            return False

        monkeypatch.setattr(auth_module, "_is_admin_user", _not_admin)
        warnings = []
        monkeypatch.setattr(
            auth_module.logger, "warning", lambda msg, *a, **kw: warnings.append(msg)
        )

        user = CurrentUser(user_id="u1", is_authenticated=True)
        assert await require_admin(current_user=user, x_admin_key="global-key") is user
        assert warnings and "ADMIN_KEY" in warnings[0]

    async def test_prod_without_admin_key_rejects(self, monkeypatch):
        settings.security.ADMIN_KEY = None
        monkeypatch.setattr(settings, "APP_ENV", "prod")

        async def _not_admin(_user):
            return False

        monkeypatch.setattr(auth_module, "_is_admin_user", _not_admin)
        user = CurrentUser(user_id="u1", is_authenticated=True)

        with pytest.raises(HTTPException) as exc:
            await require_admin(current_user=user, x_admin_key=None)
        assert exc.value.status_code == 403
        assert "create_admin" in exc.value.detail

    async def test_development_without_admin_key_allows(self, monkeypatch):
        settings.security.ADMIN_KEY = None
        monkeypatch.setattr(settings, "APP_ENV", "dev")

        async def _not_admin(_user):
            return False

        monkeypatch.setattr(auth_module, "_is_admin_user", _not_admin)
        user = CurrentUser(user_id="default", is_authenticated=True)

        assert await require_admin(current_user=user, x_admin_key=None) is user


# ---------------------------------------------------------------------------
# 3. 首个注册用户自举
# ---------------------------------------------------------------------------
class _RegisterDB:
    """注册路径：先 count(*) → 0（空库），再查重名 → None。"""

    def __init__(self, user_count: int):
        self._count = user_count
        self.added = []
        self.commits = 0

    async def scalar(self, stmt):
        if "count" in str(stmt).lower():
            return self._count
        return None

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        obj.id = obj.id or uuid.uuid4()


@pytest.fixture
def _register_env(monkeypatch):
    monkeypatch.setattr(settings.security, "SECRET_KEY", "x" * 40, raising=False)
    monkeypatch.setattr(settings.security, "AUTH_ALLOW_REGISTRATION", True, raising=False)

    async def _no_limit(*a, **kw):
        return None

    monkeypatch.setattr("src.api.auth.enforce_auth_rate_limit", _no_limit)

    async def _no_audit(**kw):
        return None

    monkeypatch.setattr("src.services.audit_service.record_audit", _no_audit)
    return SimpleNamespace(request=SimpleNamespace(client=None, headers={}))


class TestFirstUserBecomesAdmin:
    async def test_empty_db_grants_admin(self, monkeypatch, _register_env):
        db = _RegisterDB(user_count=0)
        await register(
            RegisterRequest(username="owner", password="Str0ng-Passw0rd!2026"),
            _register_env.request,
            db=db,
        )
        assert db.added and db.added[0].is_admin is True

    async def test_existing_users_get_no_admin(self, monkeypatch, _register_env):
        """存量库升级后注册的新用户不能自动变成管理员。"""
        db = _RegisterDB(user_count=3)
        await register(
            RegisterRequest(username="bob", password="Str0ng-Passw0rd!2026"),
            _register_env.request,
            db=db,
        )
        assert db.added and db.added[0].is_admin is False


# ---------------------------------------------------------------------------
# 4. create_admin CLI
# ---------------------------------------------------------------------------
def _load_cli():
    path = Path(__file__).resolve().parent.parent / "scripts" / "create_admin.py"
    spec = importlib.util.spec_from_file_location("create_admin_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestCreateAdminCli:
    @pytest.fixture(autouse=True)
    def _no_audit(self, monkeypatch):
        async def _noop(**kw):
            return None

        monkeypatch.setattr("src.services.audit_service.record_audit", _noop)

    async def test_creates_new_admin(self, monkeypatch):
        cli = _load_cli()
        session = _FakeSession(scalar_value=None)
        monkeypatch.setattr(cli, "async_session_maker", lambda: _SessionCM(session))

        code = await cli._grant("root", "Str0ng-Passw0rd!2026", promote_only=False)

        assert code == 0
        assert session.added and session.added[0].is_admin is True
        assert session.commits == 1

    async def test_promotes_existing_user(self, monkeypatch):
        cli = _load_cli()
        existing = _user(admin=False, name="alice")
        session = _FakeSession(scalar_value=existing)
        monkeypatch.setattr(cli, "async_session_maker", lambda: _SessionCM(session))

        assert await cli._grant("alice", None, promote_only=True) == 0
        assert existing.is_admin is True
        assert not session.added  # 提升不应新建用户

    async def test_idempotent_for_existing_admin(self, monkeypatch):
        cli = _load_cli()
        existing = _user(admin=True, name="alice")
        session = _FakeSession(scalar_value=existing)
        monkeypatch.setattr(cli, "async_session_maker", lambda: _SessionCM(session))

        assert await cli._grant("alice", None, promote_only=True) == 0
        assert session.commits == 0

    async def test_rejects_weak_password(self, monkeypatch):
        cli = _load_cli()
        session = _FakeSession(scalar_value=None)
        monkeypatch.setattr(cli, "async_session_maker", lambda: _SessionCM(session))

        assert await cli._grant("root", "123456", promote_only=False) == 2
        assert not session.added

    async def test_promote_missing_user_fails_loudly(self, monkeypatch):
        cli = _load_cli()
        session = _FakeSession(scalar_value=None)
        monkeypatch.setattr(cli, "async_session_maker", lambda: _SessionCM(session))

        assert await cli._grant("nobody", None, promote_only=True) == 2
        assert not session.added

    async def test_demote_warns_when_last_admin(self, monkeypatch, capsys):
        """降掉最后一个管理员 = 管理入口只剩 CLI，必须提示而不是静默。"""
        cli = _load_cli()
        existing = _user(admin=True, name="only")
        session = _FakeSession(scalar_value=existing, execute_rows=[existing.id])
        monkeypatch.setattr(cli, "async_session_maker", lambda: _SessionCM(session))

        assert await cli._revoke("only") == 0
        assert existing.is_admin is False
        assert "最后一个管理员" in capsys.readouterr().err
