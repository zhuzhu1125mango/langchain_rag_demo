"""
JWT 多用户认证单元测试（P1-1）。

覆盖：bcrypt 密码哈希、JWT 签发/校验（过期/篡改/缺配置）、
register/login 端点逻辑（依赖覆盖 + 假 DB）、get_current_user JWT 分支。
"""

import uuid

import jwt as pyjwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from src.auth import (
    _extract_user_from_jwt,
    create_access_token,
    hash_password,
    verify_jwt_token,
    verify_password,
    get_current_user,
)
from src.config import settings
from src.api.auth import register, login, me, RegisterRequest, LoginRequest

TEST_SECRET = "unit-test-secret-key-0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def restore_security_settings():
    """测试后恢复安全相关配置。"""
    saved = {
        "secret_key": settings.security.SECRET_KEY,
        "allow_registration": settings.security.AUTH_ALLOW_REGISTRATION,
        "expire_minutes": settings.security.ACCESS_TOKEN_EXPIRE_MINUTES,
        "api_key": settings.security.API_KEY,
    }
    settings.security.SECRET_KEY = TEST_SECRET
    yield
    settings.security.SECRET_KEY = saved["secret_key"]
    settings.security.AUTH_ALLOW_REGISTRATION = saved["allow_registration"]
    settings.security.ACCESS_TOKEN_EXPIRE_MINUTES = saved["expire_minutes"]
    settings.security.API_KEY = saved["api_key"]


class FakeDb:
    """假 AsyncSession：scalar 返回预设值，记录 add/commit/refresh。"""

    def __init__(self, scalar_result=None):
        self.scalar_result = scalar_result
        self.added = []
        self.committed = False

    async def scalar(self, stmt):
        return self.scalar_result

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        obj.id = uuid.uuid4()


class TestPasswordHashing:
    """bcrypt 哈希与校验。"""

    def test_roundtrip(self):
        h = hash_password("s3cret-密码")
        assert h != "s3cret-密码"
        assert verify_password("s3cret-密码", h) is True

    def test_wrong_password(self):
        h = hash_password("correct")
        assert verify_password("wrong", h) is False

    def test_invalid_hash_returns_false(self):
        assert verify_password("any", "not-a-bcrypt-hash") is False


class TestJwtToken:
    """JWT 签发与校验。"""

    def _credentials(self, token: str) -> HTTPAuthorizationCredentials:
        return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    def test_create_and_extract(self):
        user_id = str(uuid.uuid4())
        token = create_access_token(user_id)
        user = _extract_user_from_jwt(self._credentials(token))
        assert user is not None
        assert user.user_id == user_id
        assert user.is_authenticated is True

    def test_tampered_token_rejected(self):
        token = create_access_token(str(uuid.uuid4()))
        user = _extract_user_from_jwt(self._credentials(token + "x"))
        assert user is None

    def test_expired_token_rejected(self):
        user_id = str(uuid.uuid4())
        payload = {
            "sub": user_id,
            "iat": 0,
            "exp": 0,  # 已过期
        }
        token = pyjwt.encode(payload, TEST_SECRET, algorithm="HS256")
        assert _extract_user_from_jwt(self._credentials(token)) is None

    def test_missing_sub_rejected(self):
        token = pyjwt.encode({"iat": 0}, TEST_SECRET, algorithm="HS256")
        assert _extract_user_from_jwt(self._credentials(token)) is None

    def test_no_secret_key_returns_none(self):
        settings.security.SECRET_KEY = ""
        token = pyjwt.encode({"sub": "x"}, "other-secret", algorithm="HS256")
        assert _extract_user_from_jwt(self._credentials(token)) is None
        assert verify_jwt_token(token) is None
        with pytest.raises(ValueError):
            create_access_token(str(uuid.uuid4()))

    def test_verify_jwt_token_for_ws(self):
        user_id = str(uuid.uuid4())
        token = create_access_token(user_id)
        user = verify_jwt_token(token)
        assert user is not None
        assert user.user_id == user_id
        assert verify_jwt_token("garbage") is None


class TestAuthEndpoints:
    """register/login/me 端点逻辑（假 DB）。"""

    async def test_register_success(self):
        db = FakeDb(scalar_result=None)  # 用户名不存在
        resp = await register(RegisterRequest(username="alice", password="secret123"), db=db)
        assert resp.token_type == "bearer"
        assert len(db.added) == 1
        assert db.committed is True
        assert verify_password("secret123", db.added[0].password_hash)
        # token 可解出用户 id
        assert verify_jwt_token(resp.access_token).user_id == resp.user_id

    async def test_register_duplicate_username_conflict(self):
        db = FakeDb(scalar_result=uuid.uuid4())  # 用户名已存在
        with pytest.raises(HTTPException) as exc:
            await register(RegisterRequest(username="alice", password="secret123"), db=db)
        assert exc.value.status_code == 409

    async def test_register_closed_forbidden(self):
        settings.security.AUTH_ALLOW_REGISTRATION = False
        db = FakeDb()
        with pytest.raises(HTTPException) as exc:
            await register(RegisterRequest(username="alice", password="secret123"), db=db)
        assert exc.value.status_code == 403

    async def test_register_without_secret_key_unavailable(self):
        settings.security.SECRET_KEY = ""
        db = FakeDb()
        with pytest.raises(HTTPException) as exc:
            await register(RegisterRequest(username="alice", password="secret123"), db=db)
        assert exc.value.status_code == 503

    async def test_login_success(self):
        user_id = uuid.uuid4()
        db = FakeDb(scalar_result=type("U", (), {"id": user_id, "password_hash": hash_password("pw123456")})())
        resp = await login(LoginRequest(username="alice", password="pw123456"), db=db)
        assert resp.user_id == str(user_id)
        assert verify_jwt_token(resp.access_token).user_id == str(user_id)

    async def test_login_wrong_password(self):
        user_id = uuid.uuid4()
        db = FakeDb(scalar_result=type("U", (), {"id": user_id, "password_hash": hash_password("pw123456")})())
        with pytest.raises(HTTPException) as exc:
            await login(LoginRequest(username="alice", password="wrong-password"), db=db)
        assert exc.value.status_code == 401

    async def test_login_unknown_user(self):
        db = FakeDb(scalar_result=None)
        with pytest.raises(HTTPException) as exc:
            await login(LoginRequest(username="ghost", password="pw123456"), db=db)
        assert exc.value.status_code == 401

    async def test_me_with_jwt_user(self):
        from src.auth import CurrentUser

        user_id = str(uuid.uuid4())
        db = FakeDb(scalar_result=type("U", (), {"username": "alice"})())
        resp = await me(CurrentUser(user_id=user_id, is_authenticated=True), db=db)
        assert resp.user_id == user_id
        assert resp.username == "alice"

    async def test_me_with_non_uuid_user_falls_back(self):
        from src.auth import CurrentUser

        db = FakeDb()
        resp = await me(CurrentUser(user_id="api_key_user", is_authenticated=True), db=db)
        assert resp.username == "api_key_user"


class TestGetCurrentUserJwtBranch:
    """get_current_user 的 JWT 分支（API Key 优先级不变）。"""

    async def test_jwt_token_accepted(self):
        user_id = str(uuid.uuid4())
        token = create_access_token(user_id)
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        settings.security.API_KEY = None
        user = await get_current_user(api_key=None, jwt_credentials=creds)
        assert user.user_id == user_id

    async def test_invalid_jwt_raises_401(self):
        # 配置 API_KEY 以禁用开发模式匿名放行，使无效 JWT 走 401
        settings.security.API_KEY = "some-key"
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="bad-token")
        with pytest.raises(HTTPException) as exc:
            await get_current_user(api_key=None, jwt_credentials=creds)
        assert exc.value.status_code == 401

    async def test_invalid_jwt_dev_mode_falls_back_to_default(self):
        # 开发模式（未配置 API_KEY 且非 Docker）：无效 JWT 匿名放行为默认用户
        settings.security.API_KEY = None
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="bad-token")
        user = await get_current_user(api_key=None, jwt_credentials=creds)
        assert user.user_id == "default"

    async def test_api_key_takes_priority_over_jwt(self):
        settings.security.API_KEY = "key-1"
        creds = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials=create_access_token(str(uuid.uuid4()))
        )
        user = await get_current_user(api_key="key-1", jwt_credentials=creds)
        assert user.user_id == "api_key_user"
