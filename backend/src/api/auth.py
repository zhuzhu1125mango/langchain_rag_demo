"""用户认证API - Auth API（P1-1 JWT 多用户认证）

提供用户注册/登录/当前用户信息/滑动窗口刷新/用户级 API Key 接口：
1. POST /auth/register        - 注册新用户（AUTH_ALLOW_REGISTRATION 控制）
2. POST /auth/login           - 登录签发 JWT access token
3. POST /auth/refresh         - 凭未过期 token 换发新 token（滑动窗口续期）
4. GET  /auth/me              - 获取当前认证用户信息
5. POST/GET/DELETE /auth/api-keys - 用户级 API Key 发放/列举/吊销（E.6，
   USER_API_KEYS_ENABLED 控制，关闭时 404）

注意：本路由不挂全局 get_current_user 依赖（login/register/refresh 为匿名接口），
/me 与 /api-keys 由路由内自行声明依赖。
login/register/api-keys 创建带进程内滑动窗口限流（W2-9/E.6，AUTH_RATE_LIMIT_*）。
"""

from datetime import datetime, timedelta, timezone
from typing import List, Optional
import hashlib
import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select
from pydantic import BaseModel, Field
import logging

from src.auth import (
    CurrentUser,
    create_access_token,
    decode_access_token,
    get_current_user,
    hash_password,
    jwt_bearer,
    refresh_access_token,
    verify_password,
)
from src.config import settings
from src.database import get_db
from src.models import User, UserApiKey
from src.utils.rate_limit import enforce_auth_rate_limit, get_auth_rate_limits

logger = logging.getLogger("auth")

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    """注册请求。"""

    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    password: str = Field(min_length=6, max_length=128)


class LoginRequest(BaseModel):
    """登录请求。"""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    """JWT 签发响应。"""

    access_token: str
    token_type: str = "bearer"
    user_id: str


class MeResponse(BaseModel):
    """当前用户信息。"""

    user_id: str
    username: str


def _get_jwt_settings():
    """读取 JWT 相关安全配置（沿用 auth.py 的 getattr 兼容写法）。"""
    return (
        getattr(settings, "SECRET_KEY", None) or getattr(settings.security, "SECRET_KEY", ""),
        getattr(
            settings, "AUTH_ALLOW_REGISTRATION", None
        ) or getattr(settings.security, "AUTH_ALLOW_REGISTRATION", True),
    )


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(data: RegisterRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """注册新用户并直接签发 token（是否开放由 AUTH_ALLOW_REGISTRATION 控制）。"""
    login_limit, register_limit = get_auth_rate_limits()
    enforce_auth_rate_limit(request, "register", register_limit)
    secret, allow_registration = _get_jwt_settings()
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SECRET_KEY 未配置，用户认证不可用",
        )
    if not allow_registration:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="注册已关闭")

    exists = await db.scalar(select(User.id).where(User.username == data.username))
    if exists:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="用户名已存在")

    user = User(username=data.username, password_hash=hash_password(data.password))
    db.add(user)
    await db.commit()
    await db.refresh(user)

    # P2-4 审计：用户注册（best-effort，失败不阻断）
    from src.services.audit_service import record_audit
    await record_audit(
        action="auth.register",
        resource_type="user",
        resource_id=str(user.id),
        detail={"username": data.username},
    )

    logger.info(f"新用户注册: {data.username} ({user.id})")
    return TokenResponse(access_token=create_access_token(str(user.id)), user_id=str(user.id))


@router.post("/login", response_model=TokenResponse)
async def login(data: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """校验用户名密码，签发 JWT access token。"""
    login_limit, _ = get_auth_rate_limits()
    enforce_auth_rate_limit(request, "login", login_limit)
    secret, _ = _get_jwt_settings()
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SECRET_KEY 未配置，用户认证不可用",
        )

    user = await db.scalar(select(User).where(User.username == data.username))
    # 用户不存在与密码错误统一提示，避免用户名枚举
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")

    return TokenResponse(access_token=create_access_token(str(user.id)), user_id=str(user.id))


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(jwt_credentials: HTTPAuthorizationCredentials = Depends(jwt_bearer)):
    """滑动窗口刷新：凭签名有效的 access token 换发新 token（orig_iat 保留）。

    规则见 auth.refresh_access_token：允许原 token 已过有效期（401 续期场景），
    首签距今不超过 ACCESS_TOKEN_REFRESH_MAX_AGE_DAYS 天；0 = 刷新禁用。
    """
    if not jwt_credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少认证凭据",
            headers={"WWW-Authenticate": "Bearer"},
        )
    new_token = refresh_access_token(jwt_credentials.credentials)
    if not new_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录已过期，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_access_token(new_token) or {}
    return TokenResponse(access_token=new_token, user_id=str(payload.get("sub", "")))


@router.get("/me", response_model=MeResponse)
async def me(current_user: CurrentUser = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """返回当前认证用户的用户名（API Key/开发模式用户无对应记录时以 user_id 返回）。"""
    username = None
    try:
        uid = uuid.UUID(current_user.user_id)
    except (ValueError, AttributeError):
        uid = None
    if uid is not None:
        user = await db.scalar(select(User).where(User.id == uid))
        username = user.username if user else None
    return MeResponse(user_id=current_user.user_id, username=username or current_user.user_id)


# ---------------------------------------------------------------------------
# 用户级 API Key（E.6，阶段二 MCP 前置）
# ---------------------------------------------------------------------------


class ApiKeyCreateRequest(BaseModel):
    """创建用户级 API Key 请求。"""

    # 用途备注（必填），供审计追溯，如 "claude-code-office"
    name: str = Field(min_length=1, max_length=64)
    # 可选有效期（天），None = 永久
    expires_in_days: Optional[int] = Field(default=None, ge=1, le=3650)


class ApiKeyCreatedResponse(BaseModel):
    """创建成功响应——明文 key 仅在此返回一次，服务端任何接口不再返回。"""

    id: str
    name: str
    key_prefix: str
    key: str
    created_at: str
    expires_at: Optional[str] = None


class ApiKeyItem(BaseModel):
    """密钥列表条目（永不包含明文）。"""

    id: str
    name: str
    key_prefix: str
    created_at: Optional[str] = None
    last_used_at: Optional[str] = None
    expires_at: Optional[str] = None
    revoked_at: Optional[str] = None


def _require_user_api_keys_enabled() -> None:
    """开关未开启时端点整体 404（不暴露接口面）。"""
    from src.auth import _user_api_keys_enabled

    if not _user_api_keys_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="接口不存在")


def _resolve_owner_uuid(current_user: CurrentUser) -> Optional[uuid.UUID]:
    """将当前用户解析为 UUID；default/api_key_user 等非注册用户返回 None。"""
    try:
        return uuid.UUID(current_user.user_id)
    except (ValueError, AttributeError):
        return None


def _generate_api_key() -> tuple:
    """生成 (明文, SHA-256 哈希, 展示前缀)。明文仅创建响应返回一次。"""
    plain = f"rag_{secrets.token_urlsafe(32)}"
    key_hash = hashlib.sha256(plain.encode("utf-8")).hexdigest()
    return plain, key_hash, plain[:12]


def _serialize_api_key(row: UserApiKey) -> ApiKeyItem:
    """UserApiKey 行 → 列表条目（时间统一 ISO 格式，None 透传）。"""
    return ApiKeyItem(
        id=str(row.id),
        name=row.name,
        key_prefix=row.key_prefix,
        created_at=row.created_at.isoformat() if row.created_at else None,
        last_used_at=row.last_used_at.isoformat() if row.last_used_at else None,
        expires_at=row.expires_at.isoformat() if row.expires_at else None,
        revoked_at=row.revoked_at.isoformat() if row.revoked_at else None,
    )


@router.post("/api-keys", response_model=ApiKeyCreatedResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    data: ApiKeyCreateRequest,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """发放用户级 API Key（JWT 认证必须；限流 + 审计）。

    开发默认用户（default）与全局 API Key 用户（api_key_user）无真实归属，
    发放会造成「万能 Key」破坏 owner 隔离，一律拒绝（400）。
    """
    _require_user_api_keys_enabled()
    create_limit = (
        getattr(settings, "AUTH_RATE_LIMIT_API_KEY_CREATE", None)
        or getattr(settings.security, "AUTH_RATE_LIMIT_API_KEY_CREATE", 5)
    )
    enforce_auth_rate_limit(request, "api_key_create", create_limit)

    owner_uuid = _resolve_owner_uuid(current_user)
    if owner_uuid is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="当前用户为匿名/全局 Key 身份，请先注册登录后再发放 API Key",
        )

    max_keys = (
        getattr(settings, "USER_API_KEYS_MAX_PER_USER", None)
        or getattr(settings.security, "USER_API_KEYS_MAX_PER_USER", 5)
    )
    active_count = await db.scalar(
        select(func.count())
        .select_from(UserApiKey)
        .where(UserApiKey.user_id == owner_uuid, UserApiKey.revoked_at.is_(None))
    )
    if active_count is not None and active_count >= max_keys:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"已达到密钥数量上限（{max_keys}），请先吊销不再使用的密钥",
        )

    plain, key_hash, key_prefix = _generate_api_key()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=data.expires_in_days) if data.expires_in_days else None
    key_row = UserApiKey(
        user_id=owner_uuid,
        name=data.name,
        key_hash=key_hash,
        key_prefix=key_prefix,
        expires_at=expires_at,
    )
    db.add(key_row)
    await db.commit()
    await db.refresh(key_row)

    # P2-4 审计：Key 发放（best-effort，失败不阻断；不记录明文/哈希）
    from src.services.audit_service import record_audit
    await record_audit(
        action="api_key.create",
        resource_type="user_api_key",
        resource_id=str(key_row.id),
        detail={"name": data.name, "key_prefix": key_prefix, "expires_at": expires_at.isoformat() if expires_at else None},
        actor_user_id=current_user.user_id,
    )

    logger.info(f"用户 {current_user.user_id} 发放 API Key: {key_prefix}*** ({key_row.id})")
    return ApiKeyCreatedResponse(
        id=str(key_row.id),
        name=key_row.name,
        key_prefix=key_prefix,
        key=plain,
        created_at=key_row.created_at.isoformat() if key_row.created_at else now.isoformat(),
        expires_at=expires_at.isoformat() if expires_at else None,
    )


@router.get("/api-keys", response_model=List[ApiKeyItem])
async def list_api_keys(
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """列举当前用户的 API Key（仅元数据，永不返回明文）。"""
    _require_user_api_keys_enabled()
    owner_uuid = _resolve_owner_uuid(current_user)
    if owner_uuid is None:
        # 匿名/全局 Key 身份无 Key 可列，返回空列表（不报错，便于前端渲染空态）
        return []
    rows = (
        await db.execute(
            select(UserApiKey)
            .where(UserApiKey.user_id == owner_uuid)
            .order_by(UserApiKey.created_at.desc())
        )
    ).scalars().all()
    return [_serialize_api_key(row) for row in rows]


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    key_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """软吊销 API Key（下一请求即失效；仅 Key 归属人可操作）。"""
    _require_user_api_keys_enabled()
    owner_uuid = _resolve_owner_uuid(current_user)
    if owner_uuid is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权操作 API Key")
    try:
        kid = uuid.UUID(key_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="密钥不存在")

    key_row = await db.scalar(select(UserApiKey).where(UserApiKey.id == kid))
    if key_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="密钥不存在")
    if key_row.user_id != owner_uuid:
        # 403：存在但非本人（归属校验，防止越权探测他人 Key）
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权操作该密钥")
    if key_row.revoked_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该密钥已吊销")

    key_row.revoked_at = datetime.now(timezone.utc)
    await db.commit()

    from src.services.audit_service import record_audit
    await record_audit(
        action="api_key.revoke",
        resource_type="user_api_key",
        resource_id=str(key_row.id),
        detail={"name": key_row.name, "key_prefix": key_row.key_prefix},
        actor_user_id=current_user.user_id,
    )

    logger.info(f"用户 {current_user.user_id} 吊销 API Key: {key_row.key_prefix}*** ({key_row.id})")
    return None
