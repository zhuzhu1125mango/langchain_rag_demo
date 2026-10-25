"""add strategy_executions.user_id for per-owner sample isolation (SEC-1)

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-10-24

背景：学习样本此前无归属字段，`/learning/misclassification` 可读取全量用户
问题，`record-*` 也可写入他人样本（对抗性审查 P1 / SEC-1）。

本迁移：
1. 增加 `user_id`（可空）；
2. 通过 `sessions.user_id` 回填历史样本；
3. 无法回填的标记 `__legacy__`（仅后台训练使用，API 默认不返回）；
4. 设为非空并建索引。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "strategy_executions",
        sa.Column("user_id", sa.String(length=64), server_default="__legacy__", nullable=True),
    )
    op.execute(
        sa.text(
            """
            UPDATE strategy_executions AS se
            SET user_id = s.user_id
            FROM sessions AS s
            WHERE se.session_id = s.id AND se.user_id IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            "UPDATE strategy_executions SET user_id = '__legacy__' WHERE user_id IS NULL"
        )
    )
    op.alter_column(
        "strategy_executions",
        "user_id",
        existing_type=sa.String(length=64),
        nullable=False,
        server_default=None,
    )
    op.create_index(
        "ix_strategy_executions_user_id",
        "strategy_executions",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_strategy_executions_user_id", table_name="strategy_executions")
    op.drop_column("strategy_executions", "user_id")
