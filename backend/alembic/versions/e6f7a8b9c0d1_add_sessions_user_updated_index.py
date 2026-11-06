"""add index on sessions (user_id, updated_at desc)

会话列表的唯一热路径是「当前用户的会话 → 按最近活动倒序 → 分页」：

    WHERE user_id = $1 ORDER BY updated_at DESC OFFSET $2 LIMIT $3

没有索引时只能整表 Seq Scan 再 Sort。当前生产只有个位数会话，毫秒级；
加这个索引修的不是「现在慢」，而是「会话变多后列表悄悄变慢且不会报错」。

为什么是 (user_id, updated_at DESC) 而不是两个单列索引：
- user_id 是等值条件、updated_at 是排序键，复合索引一次 Index Scan 同时解决
  过滤与排序，不用额外 Sort 节点；
- 单列 (user_id) 索引仍要排序，单列 (updated_at) 在等值过滤后选择性差。
  混合方向在这里没有意义（user_id 是等值），写成 DESC 只是让意图显式。

为什么不用 CREATE INDEX CONCURRENTLY：
alembic 默认把迁移放在事务里执行，PG 会直接报
"CREATE INDEX CONCURRENTLY cannot run inside a transaction block"。
当前数据量下普通 CREATE INDEX 是毫秒级、持锁可忽略；等表真的大到需要并发建索引，
再改成 `with op.get_context().autocommit_block():`（那样就不能回滚了，是有代价的）。

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-11-07
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e6f7a8b9c0d1"
down_revision: Union[str, Sequence[str], None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_sessions_user_id_updated_at",
        "sessions",
        ["user_id", sa.text("updated_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_sessions_user_id_updated_at", table_name="sessions")
