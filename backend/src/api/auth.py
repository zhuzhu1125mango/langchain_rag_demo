"""用户认证API - Auth API（P1-1 JWT 多用户认证）

提供用户注册/登录/当前用户信息接口：
1. POST /auth/register - 注册新用户（AUTH_ALLOW_REGISTRATION 控制）
2. POST /auth/login - 登录签发 JWT access token
3. GET  /auth/me - 获取当前认证用户信息

注意：本路由不挂全局 get_current_user 依赖（login/register 为匿名接口），
/me 由路由内自行声明依赖。
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, Field
import logging
import uuid

from src.auth import (
    CurrentUser,
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)
from src.config import settings
from src.database import get_db
from src.models import User

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
async def register(data: RegisterRequest, db: AsyncSession = Depends(get_db)):
    """注册新用户并直接签发 token（是否开放由 AUTH_ALLOW_REGISTRATION 控制）。"""
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

    logger.info(f"新用户注册: {data.username} ({user.id})")
    return TokenResponse(access_token=create_access_token(str(user.id)), user_id=str(user.id))


@router.post("/login", response_model=TokenResponse)
async def login(data: LoginRequest, db: AsyncSession = Depends(get_db)):
    """校验用户名密码，签发 JWT access token。"""
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
