"""认证与授权模块。

提供基于 API Key 和 JWT 的用户认证能力，以及对象级权限校验辅助函数。
API Key 适合自托管单实例；JWT（P1-1）支持多用户注册/登录与 owner_id 隔离。
"""

import asyncio
import datetime
import hmac
import logging
from typing import Optional
import uuid

import bcrypt
import jwt as pyjwt
from fastapi import Depends, HTTPException, Security, status, WebSocket, WebSocketDisconnect
from fastapi.security import APIKeyHeader, HTTPBearer, HTTPAuthorizationCredentials
from starlette.exceptions import WebSocketException
from pydantic import BaseModel

from src.config import settings

logger = logging.getLogger(__name__)

# WS 首帧鉴权等待时长：超时未发 auth 帧视为无效客户端
WS_AUTH_TIMEOUT_SECONDS = 10.0

# JWT 算法（HS256 对称签名，SECRET_KEY 即密钥）
JWT_ALGORITHM = "HS256"

# 认证方式声明
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
admin_key_header = APIKeyHeader(name="X-Admin-Key", auto_error=False)
jwt_bearer = HTTPBearer(auto_error=False)


class CurrentUser(BaseModel):
    """当前认证用户信息。"""

    user_id: str
    is_authenticated: bool


# 单实例默认用户
_DEFAULT_USER = CurrentUser(user_id="default", is_authenticated=True)


def _verify_api_key(api_key: Optional[str]) -> bool:
    """校验 API Key 是否匹配配置（常量时间比较，防止时序攻击）。"""
    configured_key = getattr(settings, "API_KEY", None) or getattr(settings.security, "API_KEY", None)
    if not configured_key:
        return False
    return hmac.compare_digest((api_key or "").encode(), configured_key.encode())


def _get_secret_key() -> str:
    """获取 JWT 签名密钥（未配置返回空串）。"""
    return getattr(settings, "SECRET_KEY", "") or getattr(settings.security, "SECRET_KEY", "")


def hash_password(plain_password: str) -> str:
    """bcrypt 哈希明文密码。"""
    return bcrypt.hashpw(plain_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    """校验明文密码与 bcrypt 哈希是否匹配。"""
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(user_id: str) -> str:
    """为指定用户签发 JWT access token。

    Args:
        user_id: 用户 ID（即资源 owner_id）。

    Returns:
        编码后的 JWT 字符串。

    Raises:
        ValueError: SECRET_KEY 未配置时抛出。
    """
    secret = _get_secret_key()
    if not secret:
        raise ValueError("SECRET_KEY 未配置，无法签发 JWT")
    now = datetime.datetime.now(datetime.timezone.utc)
    expire_minutes = getattr(settings, "ACCESS_TOKEN_EXPIRE_MINUTES", None) or getattr(
        settings.security, "ACCESS_TOKEN_EXPIRE_MINUTES", 1440
    )
    payload = {
        "sub": user_id,
        "iat": now,
        "exp": now + datetime.timedelta(minutes=expire_minutes),
    }
    return pyjwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def _extract_user_from_jwt(credentials: HTTPAuthorizationCredentials) -> Optional[CurrentUser]:
    """从 JWT token 中提取用户信息。

    校验签名与有效期；不查库（无会话表，删号后 token 自然过期失效）。

    Returns:
        CurrentUser: 校验通过时返回；无效/过期/SECRET_KEY 未配置时返回 None。
    """
    secret = _get_secret_key()
    if not secret:
        return None
    try:
        payload = pyjwt.decode(credentials.credentials, secret, algorithms=[JWT_ALGORITHM])
    except pyjwt.PyJWTError:
        return None
    sub = payload.get("sub")
    if not sub:
        return None
    return CurrentUser(user_id=str(sub), is_authenticated=True)


def verify_jwt_token(token: str) -> Optional[CurrentUser]:
    """校验裸 token 字符串（WebSocket 首帧鉴权用）。"""
    secret = _get_secret_key()
    if not secret:
        return None
    try:
        payload = pyjwt.decode(token, secret, algorithms=[JWT_ALGORITHM])
    except pyjwt.PyJWTError:
        return None
    sub = payload.get("sub")
    if not sub:
        return None
    return CurrentUser(user_id=str(sub), is_authenticated=True)


def validate_uuid_user_id(user_id: str) -> bool:
    """校验 user_id 是否为合法 UUID（JWT 用户应来自 users 表主键）。"""
    try:
        uuid.UUID(user_id)
        return True
    except (ValueError, AttributeError):
        return False


async def get_current_user(
    api_key: Optional[str] = Security(api_key_header),
    jwt_credentials: Optional[HTTPAuthorizationCredentials] = Security(jwt_bearer),
) -> CurrentUser:
    """获取当前认证用户。

    校验顺序：
    1. X-API-Key 头；
    2. Authorization: Bearer <JWT>（有效则返回对应用户）；
    3. 未配置 API_KEY 且非 Docker 环境时，返回默认用户（开发模式匿名放行）。

    Docker 生产环境必须配置 API_KEY，否则除有效 JWT 外拒绝所有请求。

    Args:
        api_key: API Key 头内容。
        jwt_credentials: JWT Bearer 凭证。

    Returns:
        CurrentUser: 当前用户信息。

    Raises:
        HTTPException: 认证失败时抛出 401。
    """
    configured_key = getattr(settings, "API_KEY", None) or getattr(settings.security, "API_KEY", None)

    if api_key and _verify_api_key(api_key):
        return CurrentUser(user_id="api_key_user", is_authenticated=True)

    if jwt_credentials:
        user = _extract_user_from_jwt(jwt_credentials)
        if user:
            return user

    # 未配置 API_KEY 且非 Docker 环境：开发模式允许匿名访问。
    # 注意置于 JWT 校验之后，避免已登录 JWT 用户被并入默认用户。
    if not configured_key and not settings.IN_DOCKER:
        return _DEFAULT_USER

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无效的认证凭据",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user_for_ws(websocket: WebSocket) -> CurrentUser:
    """WebSocket 首帧鉴权。

    浏览器 WebSocket 无法携带自定义请求头，而 query parameter 传密钥会进入
    反向代理访问日志与浏览器历史。改为连接建立后由客户端首帧发送
    ``{"type": "auth", "api_key": "..."}`` 完成认证，认证通过前不推送任何业务数据。

    流程：
    1. accept 接受连接（握手阶段不校验，凭据不落 URL）；
    2. 开发模式（未配置 API_KEY 且非 Docker）直接放行；
    3. 等待首帧 auth（超时 ``WS_AUTH_TIMEOUT_SECONDS`` 秒）；
    4. 校验通过发送 ``{"type": "auth_ok"}``，失败以 1008 关闭连接。

    Args:
        websocket: FastAPI WebSocket 对象。

    Returns:
        CurrentUser: 当前认证用户信息。

    Raises:
        WebSocketException: 认证失败/超时时抛出（连接已以 1008 关闭），
            用于终止端点协程。
    """
    await websocket.accept()

    configured_key = getattr(settings, "API_KEY", None) or getattr(settings.security, "API_KEY", None)

    # 未配置 API_KEY 且非 Docker 环境：开发模式允许匿名访问
    if not configured_key and not settings.IN_DOCKER:
        await websocket.send_json({"type": "auth_ok"})
        return _DEFAULT_USER

    try:
        first_frame = await asyncio.wait_for(
            websocket.receive_json(), timeout=WS_AUTH_TIMEOUT_SECONDS
        )
    except (asyncio.TimeoutError, TimeoutError):
        await websocket.close(code=1008, reason="认证超时")
        raise WebSocketException(code=1008, reason="认证超时")
    except WebSocketDisconnect:
        # 客户端在鉴权前断开，无需再发 close 帧
        raise WebSocketException(code=1008, reason="连接已断开")
    except Exception:
        # 非 JSON 帧 / 非 dict 帧
        await websocket.close(code=1008, reason="无效的认证帧")
        raise WebSocketException(code=1008, reason="无效的认证帧")

    api_key = first_frame.get("api_key") if isinstance(first_frame, dict) else None
    if api_key and _verify_api_key(api_key):
        await websocket.send_json({"type": "auth_ok"})
        return CurrentUser(user_id="api_key_user", is_authenticated=True)

    # JWT 用户：首帧 {"type": "auth", "token": "<access_token>"}
    token = first_frame.get("token") if isinstance(first_frame, dict) else None
    if token:
        user = verify_jwt_token(token)
        if user:
            await websocket.send_json({"type": "auth_ok"})
            return user

    await websocket.close(code=1008, reason="无效的认证凭据")
    raise WebSocketException(code=1008, reason="无效的认证凭据")


def require_owner(owner_id: str, current_user: CurrentUser) -> None:
    """校验当前用户是否为资源所有者（per-user 隔离）。

    规则：
    - 资源 owner_id 与当前用户 ID 必须一致（含 api_key_user，创建资源时
      owner_id 一律记录为当前用户 ID，因此单租户 API Key 形态天然自洽）；
    - owner_id 为空的遗留数据保持放行，避免历史数据被锁死
      （可用 scripts/migrate_owner_id.py 补齐）；
    - 开发模式默认用户（default）在非 Docker 环境放行所有资源。

    Args:
        owner_id: 资源所有者 ID。
        current_user: 当前认证用户。

    Raises:
        HTTPException: 无权限时抛出 403。
    """
    if not current_user.is_authenticated:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    if owner_id and owner_id != current_user.user_id:
        # 开发模式默认用户可访问所有资源（向后兼容）
        if current_user.user_id == "default" and not settings.IN_DOCKER:
            return
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该资源")


async def require_admin(
    current_user: CurrentUser = Depends(get_current_user),
    x_admin_key: Optional[str] = Security(admin_key_header),
) -> CurrentUser:
    """管理操作鉴权（全局配置修改、指标重置等）。

    规则：
    - 配置了 ``ADMIN_KEY``：请求须携带匹配的 ``X-Admin-Key`` 头（常量时间比较）；
    - 未配置 ``ADMIN_KEY``：仅开发模式放行，生产模式一律 403，
      避免任何认证用户篡改全局运行时配置。
    """
    admin_key = getattr(settings.security, "ADMIN_KEY", None)
    if admin_key:
        if x_admin_key and hmac.compare_digest(x_admin_key.encode(), admin_key.encode()):
            return current_user
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要有效的 X-Admin-Key")
    if settings.IS_PRODUCTION:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="生产环境执行管理操作需配置 ADMIN_KEY 并携带 X-Admin-Key 头",
        )
    return current_user
