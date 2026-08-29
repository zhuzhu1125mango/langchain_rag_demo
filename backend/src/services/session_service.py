"""会话服务 - 跨 API 复用的会话写操作。

集中存放会话消息的原子追加实现，保证所有写路径走同一并发安全入口。
"""

import logging
import uuid
from datetime import datetime

from sqlalchemy import func, update, bindparam
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import Session as SessionModel

logger = logging.getLogger("rag_system")


async def append_session_message(db: AsyncSession, session_id: uuid.UUID, payload: dict) -> int:
    """原子追加一条消息到会话，返回追加后的消息总数。

    使用 PostgreSQL 的 ``jsonb || jsonb`` 运算符在服务端拼接，不经 ORM
    读改写：两条并发 UPDATE 在行锁下串行执行，各自追加的元素都会保留，
    彻底消除「A 读 → B 读 → A 写 → B 写覆盖 A」的丢消息窗口。

    注意：
    - messages 列必须为 JSONB 类型（旧 JSON 列请先运行
      scripts/migrate_session_messages_jsonb.py）；
    - 本语句直接落库但不提交，由调用方决定事务边界；
    - 调用方如需最新 messages 列表需重新 SELECT。
    """
    stmt = (
        update(SessionModel)
        .where(SessionModel.id == session_id)
        .values(
            messages=SessionModel.messages
            + bindparam("m", value=[payload], type_=JSONB),
            updated_at=datetime.now(),
        )
        .returning(func.jsonb_array_length(SessionModel.messages))
        .execution_options(synchronize_session=False)
    )
    result = await db.execute(stmt)
    count = result.scalar()
    logger.debug(f"原子追加消息成功: session={session_id}, messages_count={count}")
    return count
