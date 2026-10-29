"""SEC-14 账号注销的缓存失效回归测试。

背景：`delete_user_account` 曾从 `src.services.cache_service` 导入
`invalidate_kb_list_cache`（该函数实际在 `src.api.knowledge_base`），
ImportError 又被 `except Exception: pass` 静默吞掉 —— 注销后知识库列表缓存
从未失效，前端会继续显示已删除的知识库。本测试锁定修复。
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services import account_service


class _FakeResult:
    """模拟 SQLAlchemy Result：注销链路只需要 scalars().all() 与 rowcount。"""

    def __init__(self, rows=None, rowcount=0):
        self._rows = rows or []
        self.rowcount = rowcount

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeDB:
    def __init__(self):
        self.commits = 0

    async def execute(self, stmt):
        # 查询（select）返回空集合；删除（delete）返回 rowcount=0
        return _FakeResult()

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_delete_user_account_invalidates_kb_list_cache(monkeypatch):
    """注销必须真正触发知识库列表缓存失效（空知识库时也应调用一次）。"""
    calls = []

    async def _record(user_id):
        calls.append(user_id)

    monkeypatch.setattr(
        "src.api.knowledge_base.invalidate_kb_list_cache", _record, raising=False
    )
    # 向量库 / MinIO 为外部依赖：置为可用的假单例，避免真实连接
    monkeypatch.setattr(
        "src.services.vector_store.VectorStoreManager.get_instance",
        AsyncMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        "src.services.minio_service.MinioService.get_instance",
        AsyncMock(return_value=MagicMock()),
    )

    user_id = str(uuid.uuid4())
    summary = await account_service.delete_user_account(_FakeDB(), user_id)

    assert calls == [user_id], "注销后未失效知识库列表缓存"
    assert summary["users"] == 0  # 假 DB 无行可删，计数应为 0 而不是抛错


@pytest.mark.asyncio
async def test_kb_list_cache_failure_is_logged_not_raised(monkeypatch, caplog):
    """缓存失效失败不能阻断注销，但必须留下可恢复级记录（不再静默 pass）。"""
    import logging

    async def _boom(user_id):
        raise RuntimeError("cache down")

    monkeypatch.setattr(
        "src.api.knowledge_base.invalidate_kb_list_cache", _boom, raising=False
    )
    monkeypatch.setattr(
        "src.services.vector_store.VectorStoreManager.get_instance",
        AsyncMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        "src.services.minio_service.MinioService.get_instance",
        AsyncMock(return_value=MagicMock()),
    )

    with caplog.at_level(logging.DEBUG, logger="rag_system"):
        await account_service.delete_user_account(_FakeDB(), str(uuid.uuid4()))

    hits = [r for r in caplog.records if "account.delete.invalidate_kb_list_cache" in r.getMessage()]
    assert hits, "注销时缓存失效失败未记录"
    assert hits[-1].levelno == logging.WARNING
