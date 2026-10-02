"""认证与授权模块。

提供基于 API Key 和 JWT 的用户认证能力，以及对象级权限校验辅助函数。
API Key 适合自托管单实例；JWT（P1-1）支持多用户注册/登录与 owner_id 隔离。
"""

import asyncio
import datetime
import hashlib
import hmac
import logging
from typing import Optional
import uuid

import bcrypt
import jwt as pyjwt
from fastapi import Depends, HTTPException, Security, status, WebSocket, WebSocketDisconnect
from fastapi.security import APIKeyHeader, HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import select
from starlette.exceptions import WebSocketException
from pydantic import BaseModel

from src.config import settings

logger = logging.getLogger(__name__)

# WS 首帧鉴权等待时长：超时未发 auth 帧视为无效客户端
WS_AUTH_TIMEOUT_SECONDS = 10.0

# JWT 算法（HS256 对称签名，SECRET_KEY 即密钥）
JWT_ALGORITHM = "HS256"

# 用户级 API Key last_used_at 写库节流：距上次记录超过该秒数才 UPDATE，
# 防止每个认证请求都写库（泄露感知线索只需分钟级精度）
API_KEY_LAST_USED_THROTTLE_SECONDS = 60.0

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


def _user_api_keys_enabled() -> bool:
    """读取用户级 API Key 开关（E.6；请求时求值，便于测试覆盖）。"""
    return bool(
        getattr(settings, "USER_API_KEYS_ENABLED", None)
        or getattr(settings.security, "USER_API_KEYS_ENABLED", False)
    )


async def _resolve_api_key(api_key: str) -> Optional[CurrentUser]:
    """解析用户级 API Key（E.6）。

    在全局 API_KEY 未命中后调用：按 SHA-256 哈希查 user_api_keys，
    命中有效（未吊销/未过期）Key 则返回绑定用户——user_id 即资源
    owner_id，require_owner / KB 归属校验等隔离链路与 JWT 完全一致。

    - 开关关闭时直接返回 None（零查询开销）；
    - 使用一次性短会话，不占用请求级 DB 会话（P1-9/P1-10 教训）；
    - last_used_at 按 API_KEY_LAST_USED_THROTTLE_SECONDS 节流更新，
      写库失败 best-effort 不影响认证结果。

    Args:
        api_key: X-API-Key 头的明文内容。

    Returns:
        CurrentUser: 命中有效 Key 时返回绑定用户；否则 None。
    """
    if not _user_api_keys_enabled():
        return None
    from src.database import async_session_maker
    from src.models import UserApiKey

    key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    now = datetime.datetime.now(datetime.timezone.utc)
    async with async_session_maker() as db:
        try:
            key_row = await db.scalar(select(UserApiKey).where(UserApiKey.key_hash == key_hash))
        except Exception as e:  # noqa: BLE001 —— 认证链 DB 故障按未命中处理，不向调用方扩散
            logger.warning(f"用户级 API Key 查询失败（按未命中处理）: {e}")
            return None
        if key_row is None or not key_row.is_valid(now):
            return None
        # last_used_at 节流更新；SQLite 测试库读回 naive datetime，统一按 UTC 补时区再相减
        last_used = key_row.last_used_at
        if last_used is not None and last_used.tzinfo is None:
            last_used = last_used.replace(tzinfo=datetime.timezone.utc)
        if last_used is None or (now - last_used).total_seconds() > API_KEY_LAST_USED_THROTTLE_SECONDS:
            key_row.last_used_at = now
            try:
                await db.commit()
            except Exception as e:  # noqa: BLE001
                await db.rollback()
                logger.warning(f"API Key last_used_at 更新失败（已忽略）: {e}")
        return CurrentUser(user_id=str(key_row.user_id), is_authenticated=True)


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


def create_access_token(user_id: str, orig_iat: Optional[datetime.datetime] = None) -> str:
    """为指定用户签发 JWT access token。

    Args:
        user_id: 用户 ID（即资源 owner_id）。
        orig_iat: 刷新链首签时间（None = 新登录，取当前时间）。
            /auth/refresh 换发时透传，保证滑动窗口（ACCESS_TOKEN_REFRESH_MAX_AGE_DAYS）
            从首次登录起算而非从每次刷新起算。

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
        settings.security, "ACCESS_TOKEN_EXPIRE_MINUTES", 60
    )
    # NumericDate（epoch 秒）：pyjwt 2.13 起不再自动序列化 datetime
    payload = {
        "sub": user_id,
        "iat": int(now.timestamp()),
        "exp": int((now + datetime.timedelta(minutes=expire_minutes)).timestamp()),
        "orig_iat": int((orig_iat or now).timestamp()),
    }
    return pyjwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    """解码并校验 JWT（签名 + 有效期），返回 payload；无效返回 None。"""
    secret = _get_secret_key()
    if not secret:
        return None
    try:
        return pyjwt.decode(token, secret, algorithms=[JWT_ALGORITHM])
    except pyjwt.PyJWTError:
        return None


def refresh_access_token(token: str) -> Optional[str]:
    """滑动窗口刷新：凭签名有效的 token 换发新 token。

    规则：
    - 仅校验签名（options 关闭 verify_exp）：允许原 token 已过有效期——
      前端 401 触发续期时手里的正是过期 token，若此处仍校验 exp 则续期
      永远无法救回过期会话；
    - 滑动窗口硬上限：首签时间取 orig_iat（旧 token 无该字段时回退 iat，
      向后兼容），距今超过 ACCESS_TOKEN_REFRESH_MAX_AGE_DAYS 天即拒绝，
      因此被盗 token 最长存活受该窗口约束而非无限续期；
    - ACCESS_TOKEN_REFRESH_MAX_AGE_DAYS=0 时刷新整体禁用。

    Returns:
        新签发的 JWT（orig_iat 保留）；不可刷新时返回 None。
    """
    secret = _get_secret_key()
    if not secret:
        return None
    try:
        payload = pyjwt.decode(
            token, secret, algorithms=[JWT_ALGORITHM], options={"verify_exp": False}
        )
    except pyjwt.PyJWTError:
        return None
    sub = payload.get("sub")
    if not sub:
        return None
    max_age_days = getattr(settings, "ACCESS_TOKEN_REFRESH_MAX_AGE_DAYS", None) or getattr(
        settings.security, "ACCESS_TOKEN_REFRESH_MAX_AGE_DAYS", 7
    )
    if max_age_days <= 0:
        return None
    orig_iat = payload.get("orig_iat") or payload.get("iat")
    if orig_iat is None:
        return None
    try:
        orig_dt = datetime.datetime.fromtimestamp(int(orig_iat), tz=datetime.timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    if now - orig_dt > datetime.timedelta(days=max_age_days):
        return None
    return create_access_token(str(sub), orig_iat=orig_dt)


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
    3. 未配置 API_KEY 且非生产环境时，返回默认用户（开发模式匿名放行）。

    生产环境（含非 Docker 裸跑 APP_ENV=prod）必须配置 API_KEY，
    否则除有效 JWT 外拒绝所有请求。

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

    # 用户级 API Key（E.6）：全局 Key 未命中时按哈希解析绑定用户。
    # 仅在携带 X-API-Key 头且开关开启时才产生一次 DB 查询，JWT 主路径零开销。
    if api_key:
        user = await _resolve_api_key(api_key)
        if user:
            return user

    if jwt_credentials:
        user = _extract_user_from_jwt(jwt_credentials)
        if user:
            return user

    # 未配置 API_KEY 且非生产环境：开发模式允许匿名访问。
    # 按 IS_PRODUCTION（APP_ENV）判定而非 IN_DOCKER：非 Docker 裸跑的
    # 生产部署（APP_ENV=prod）同样拒绝匿名，堵住绕过 Docker 判定的缺口。
    # 注意置于 JWT 校验之后，避免已登录 JWT 用户被并入默认用户。
    if not configured_key and not settings.IS_PRODUCTION:
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
    2. 开发模式（未配置 API_KEY 且非生产环境）直接放行；
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

    # 未配置 API_KEY 且非生产环境：开发模式允许匿名访问（判定口径同 get_current_user）
    if not configured_key and not settings.IS_PRODUCTION:
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
    except (ValueError, RuntimeError):
        # 非 JSON 帧（receive_json 抛 JSONDecodeError/ValueError）或二进制帧
        # （starlette 抛 RuntimeError）；断开/超时已由上方分支单独捕获
        await websocket.close(code=1008, reason="无效的认证帧")
        raise WebSocketException(code=1008, reason="无效的认证帧")

    api_key = first_frame.get("api_key") if isinstance(first_frame, dict) else None
    if api_key and _verify_api_key(api_key):
        await websocket.send_json({"type": "auth_ok"})
        return CurrentUser(user_id="api_key_user", is_authenticated=True)

    # 用户级 API Key（E.6）：全局 Key 未命中时按哈希解析绑定用户
    if api_key:
        user = await _resolve_api_key(api_key)
        if user:
            await websocket.send_json({"type": "auth_ok"})
            return user

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
    - owner_id 为空的遗留数据：开发模式兼容放行；生产（Docker）拒绝访问，
      需用 scripts/migrate_owner_id.py 补齐后访问；
    - 开发模式默认用户（default）在非 Docker 环境放行所有资源。

    Args:
        owner_id: 资源所有者 ID。
        current_user: 当前认证用户。

    Raises:
        HTTPException: 无权限时抛出 403。
    """
    if not current_user.is_authenticated:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    if not owner_id:
        # 遗留数据（owner_id 为空）：开发模式兼容放行；生产环境收紧拒绝，
        # 避免无主资源被任意已登录用户访问，须先迁移补齐归属。
        if settings.IN_DOCKER:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="该资源缺少所有者信息，请迁移补齐后访问")
        return
    if owner_id != current_user.user_id:
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
