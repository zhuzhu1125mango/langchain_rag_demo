"""认证与授权模块。

提供基于 API Key 和 JWT 的用户认证能力，以及对象级权限校验辅助函数。
当前阶段以 API Key 认证为主（适合自托管单实例），JWT 接口预留以便后续扩展多用户。
"""

import hmac
import logging
from typing import Optional
from fastapi import Depends, HTTPException, Security, status, WebSocket
from fastapi.security import APIKeyHeader, HTTPBearer, HTTPAuthorizationCredentials
from starlette.exceptions import WebSocketException
from pydantic import BaseModel

from src.config import settings

logger = logging.getLogger(__name__)

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


def _extract_user_from_jwt(credentials: HTTPAuthorizationCredentials) -> Optional[CurrentUser]:
    """从 JWT token 中提取用户信息（预留接口）。

    当前未启用完整 JWT 签发/校验逻辑，仅做占位；后续可接入 python-jose。
    """
    # TODO: 实现 JWT 校验
    return None


async def get_current_user(
    api_key: Optional[str] = Security(api_key_header),
    jwt_credentials: Optional[HTTPAuthorizationCredentials] = Security(jwt_bearer),
) -> CurrentUser:
    """获取当前认证用户。

    校验顺序：
    1. X-API-Key 头；
    2. Authorization: Bearer <JWT>；
    3. 未配置 API_KEY 且非 Docker 环境时，返回默认用户（开发模式）。

    Docker 生产环境必须配置 API_KEY，否则拒绝所有请求。

    Args:
        api_key: API Key 头内容。
        jwt_credentials: JWT Bearer 凭证。

    Returns:
        CurrentUser: 当前用户信息。

    Raises:
        HTTPException: 认证失败时抛出 401。
    """
    configured_key = getattr(settings, "API_KEY", None) or getattr(settings.security, "API_KEY", None)

    # 未配置 API_KEY 且非 Docker 环境：开发模式允许匿名访问
    if not configured_key and not settings.IN_DOCKER:
        return _DEFAULT_USER

    if api_key and _verify_api_key(api_key):
        return CurrentUser(user_id="api_key_user", is_authenticated=True)

    if jwt_credentials:
        user = _extract_user_from_jwt(jwt_credentials)
        if user:
            return user

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无效的认证凭据",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user_for_ws(websocket: WebSocket) -> CurrentUser:
    """从 WebSocket 查询参数中获取并校验 API Key。

    浏览器 WebSocket API 不支持自定义请求头，因此复用 query parameter 传递 api_key。
    Docker 生产环境必须配置 API_KEY，否则拒绝所有 WebSocket 连接。

    Args:
        websocket: FastAPI WebSocket 对象。

    Returns:
        CurrentUser: 当前认证用户信息。

    Raises:
        WebSocketException: 认证失败时抛出，并先发送 close 帧断开连接。
    """
    configured_key = getattr(settings, "API_KEY", None) or getattr(settings.security, "API_KEY", None)

    # 未配置 API_KEY 且非 Docker 环境：开发模式允许匿名访问
    if not configured_key and not settings.IN_DOCKER:
        return _DEFAULT_USER

    api_key = websocket.query_params.get("api_key")
    if api_key and _verify_api_key(api_key):
        return CurrentUser(user_id="api_key_user", is_authenticated=True)

    # 认证失败，发送 close 帧后抛出异常终止连接建立
    await websocket.close(code=1008, reason="无效的认证凭据")
    raise WebSocketException(code=1008, reason="无效的认证凭据")


def require_owner(owner_id: str, current_user: CurrentUser) -> None:
    """校验当前用户是否为资源所有者。

    Args:
        owner_id: 资源所有者 ID。
        current_user: 当前认证用户。

    Raises:
        HTTPException: 无权限时抛出 403。
    """
    if not current_user.is_authenticated:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    if owner_id and owner_id != current_user.user_id and current_user.user_id != "api_key_user":
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
