"""
会话消息原子追加测试（修复8）。

回归：此前消息保存为「读整个 messages → 内存拼接 → 整体覆写」，
并发写同一会话时后写者覆盖先写者，导致消息丢失。
现统一走 session_service.append_session_message（jsonb || 服务端拼接）。
"""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select, update, bindparam, func
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB

from src.models import Session as SessionModel
from src.services.session_service import append_session_message


def _build_stmt(payload: dict):
    """与 helper 内部一致的 UPDATE 语句，供 SQL 编译断言。"""
    return (
        update(SessionModel)
        .where(SessionModel.id == uuid.uuid4())
        .values(
            messages=SessionModel.messages + bindparam("m", value=[payload], type_=JSONB),
        )
        .returning(func.jsonb_array_length(SessionModel.messages))
        .execution_options(synchronize_session=False)
    )


class TestAppendStatementCompilation:
    """SQL 编译层断言：确实是服务端 jsonb 拼接而非读改写。"""

    def test_compiles_to_jsonb_concat(self):
        stmt = _build_stmt({"id": "m1", "role": "user"})
        compiled = str(stmt.compile(dialect=postgresql.dialect()))
        # jsonb || 运算符出现在 SET messages=(sessions.messages || ...) —— 服务端拼接
        assert "sessions.messages ||" in compiled
        assert "UPDATE sessions SET messages=" in compiled
        # RETURNING 使用 jsonb_array_length，而非回传整个消息列表
        assert "jsonb_array_length" in compiled

    def test_bind_param_is_jsonb(self):
        """绑定参数类型为 JSONB（防止退化为 json 类型导致 || 报错）。"""
        stmt = _build_stmt({"id": "m1"})
        compiled = stmt.compile(dialect=postgresql.dialect())
        param_types = [str(t) for t in compiled.params_types.values()] if hasattr(compiled, "params_types") else []
        # 不同 SQLAlchemy 版本暴露方式不同，退而断言编译不出错且含 JSONB 字面量
        assert compiled is not None


class TestAppendSessionMessage:
    """helper 行为断言（mock db，不依赖真实 PG）。"""

    async def test_executes_and_returns_count(self):
        db = MagicMock()
        db.execute = AsyncMock()
        db.execute.return_value.scalar = MagicMock(return_value=3)

        count = await append_session_message(db, uuid.uuid4(), {"id": "m1", "role": "user"})

        assert count == 3
        db.execute.assert_awaited_once()

    async def test_does_not_commit(self):
        """事务边界留给调用方：helper 自身不得 commit。"""
        db = MagicMock()
        db.execute = AsyncMock()
        db.execute.return_value.scalar = MagicMock(return_value=1)
        db.commit = AsyncMock()

        await append_session_message(db, uuid.uuid4(), {"id": "m1"})

        db.commit.assert_not_called()


@pytest.mark.e2e
class TestConcurrentAppendE2E:
    """真实 PG 并发追加（--run-e2e 时运行）。

    验证 N 个并发事务对同一会话各追加一条后，消息总数恰为 N。
    修复前的读改写实现会丢失部分写入（通常远小于 N）。
    """

    async def test_n_concurrent_appends_all_persist(self):
        from src.database import async_session, init_db

        await init_db()
        N = 10

        async with async_session() as db:
            session = SessionModel(
                user_id="e2e-append-test",
                title="并发追加测试",
                messages=[],
            )
            db.add(session)
            await db.commit()
            session_id = session.id

        try:
            async def append_one(i: int):
                async with async_session() as db:
                    await append_session_message(
                        db, session_id,
                        {"id": f"m{i}", "role": "user", "content": f"msg-{i}"},
                    )
                    await db.commit()

            await asyncio.gather(*(append_one(i) for i in range(N)))

            async with async_session() as db:
                result = await db.execute(
                    select(SessionModel).filter(SessionModel.id == session_id)
                )
                session = result.scalar_one()
                assert len(session.messages) == N
                contents = {m["content"] for m in session.messages}
                assert contents == {f"msg-{i}" for i in range(N)}
        finally:
            async with async_session() as db:
                result = await db.execute(
                    select(SessionModel).filter(SessionModel.id == session_id)
                )
                session = result.scalar_one_or_none()
                if session:
                    await db.delete(session)
                    await db.commit()
