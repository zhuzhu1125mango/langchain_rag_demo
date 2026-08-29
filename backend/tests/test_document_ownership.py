"""
文档端点对象级授权（owner 校验）单元测试。

验证 _get_owned_document 辅助函数的鉴权语义：
- 非法 ID → 400；不存在 → 404；非所有者 → 403
- per-user 隔离：api_key_user 不豁免（仅能访问 owner_id=api_key_user 的资源）；
  dev 默认用户仅在非 Docker 开发模式豁免

不依赖真实数据库/向量库，可随单元测试全量运行。
"""

import uuid as uuid_mod
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.api.document import _get_owned_document
from src.auth import CurrentUser


def _make_db(doc):
    """构造一个 execute 后返回指定文档（或 None）的 mock AsyncSession。"""
    result = SimpleNamespace(scalar_one_or_none=lambda: doc)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _doc(owner_id="user_a"):
    return SimpleNamespace(id=uuid_mod.uuid4(), owner_id=owner_id, filename="a.txt")


class TestGetOwnedDocument:
    """_get_owned_document 鉴权语义。"""


    async def test_invalid_uuid_returns_400(self):
        db = _make_db(None)
        user = CurrentUser(user_id="user_a", is_authenticated=True)
        with pytest.raises(HTTPException) as exc:
            await _get_owned_document(db, "not-a-uuid", user)
        assert exc.value.status_code == 400


    async def test_missing_doc_returns_404(self):
        db = _make_db(None)
        user = CurrentUser(user_id="user_a", is_authenticated=True)
        with pytest.raises(HTTPException) as exc:
            await _get_owned_document(db, str(uuid_mod.uuid4()), user)
        assert exc.value.status_code == 404


    async def test_other_users_doc_returns_403(self):
        """核心回归用例：普通用户访问他人文档必须被拒绝（IDOR 防护）。"""
        db = _make_db(_doc(owner_id="user_a"))
        intruder = CurrentUser(user_id="user_b", is_authenticated=True)
        with pytest.raises(HTTPException) as exc:
            await _get_owned_document(db, str(uuid_mod.uuid4()), intruder)
        assert exc.value.status_code == 403


    async def test_owner_can_access(self):
        doc = _doc(owner_id="user_a")
        db = _make_db(doc)
        owner = CurrentUser(user_id="user_a", is_authenticated=True)
        result = await _get_owned_document(db, str(uuid_mod.uuid4()), owner)
        assert result is doc


    async def test_api_key_user_isolated(self):
        """api_key_user 不再豁免对象级校验：访问他人资源返回 403（per-user 隔离）。"""
        doc = _doc(owner_id="user_a")
        db = _make_db(doc)
        api_user = CurrentUser(user_id="api_key_user", is_authenticated=True)
        with pytest.raises(HTTPException) as exc:
            await _get_owned_document(db, str(uuid_mod.uuid4()), api_user)
        assert exc.value.status_code == 403


    async def test_api_key_user_can_access_own_resource(self):
        """api_key_user 访问自己名下（owner_id=api_key_user）的资源放行。"""
        doc = _doc(owner_id="api_key_user")
        db = _make_db(doc)
        api_user = CurrentUser(user_id="api_key_user", is_authenticated=True)
        result = await _get_owned_document(db, str(uuid_mod.uuid4()), api_user)
        assert result is doc


    async def test_dev_default_user_bypasses_ownership(self, monkeypatch):
        """非 Docker 开发模式：default 用户豁免（向后兼容）。"""
        monkeypatch.setattr("src.auth.settings.IN_DOCKER", False)
        doc = _doc(owner_id="someone_else")
        db = _make_db(doc)
        dev_user = CurrentUser(user_id="default", is_authenticated=True)
        result = await _get_owned_document(db, str(uuid_mod.uuid4()), dev_user)
        assert result is doc


    async def test_default_user_in_docker_is_restricted(self, monkeypatch):
        """Docker 生产模式：default 用户不再豁免。"""
        monkeypatch.setattr("src.auth.settings.IN_DOCKER", True)
        db = _make_db(_doc(owner_id="someone_else"))
        dev_user = CurrentUser(user_id="default", is_authenticated=True)
        with pytest.raises(HTTPException) as exc:
            await _get_owned_document(db, str(uuid_mod.uuid4()), dev_user)
        assert exc.value.status_code == 403
