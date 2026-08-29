"""
知识库默认标志作用域单元测试。

回归用例：set_single_default 的「清空默认标志」UPDATE 必须限定 owner，
不允许跨用户重置其他用户的默认知识库。

不依赖真实数据库，可随单元测试全量运行。
"""

import uuid as uuid_mod
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.api.knowledge_base import set_single_default


@pytest.mark.asyncio
async def test_set_default_scopes_update_by_owner():
    """清空 is_default 的 UPDATE 语句必须包含 owner_id 过滤条件。"""
    db = AsyncMock()
    kb_id = uuid_mod.uuid4()
    kb = SimpleNamespace(id=kb_id, is_default=False)
    # 第二次 execute（SELECT 目标知识库）返回 kb
    select_result = SimpleNamespace(scalar_one_or_none=lambda: kb)
    db.execute = AsyncMock(side_effect=[AsyncMock(), select_result])

    await set_single_default(db, kb_id, owner_id="user_a")

    # 首个 execute 是 UPDATE 语句
    update_stmt = db.execute.call_args_list[0].args[0]
    compiled = str(update_stmt.compile(compile_kwargs={"literal_binds": True}))

    assert "owner_id" in compiled, (
        "UPDATE 必须限定 owner_id，否则会跨用户重置默认知识库（IDOR 回归）"
    )
    assert "user_a" in compiled
    # 目标知识库被置为默认
    assert kb.is_default is True
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_default_works_when_kb_missing():
    """目标知识库已被并发删除时不报错，仅完成清空操作。"""
    db = AsyncMock()
    empty_result = SimpleNamespace(scalar_one_or_none=lambda: None)
    db.execute = AsyncMock(side_effect=[AsyncMock(), empty_result])

    await set_single_default(db, uuid_mod.uuid4(), owner_id="user_a")

    db.commit.assert_awaited_once()
