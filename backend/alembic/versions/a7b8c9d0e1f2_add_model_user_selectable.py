"""add model_deployments.user_selectable（用户可选模型）

为什么需要这一列：在这之前「用哪个模型」只有管理员能决定（全局路由），
终端用户看到的就是唯一答案。要让用户在多个模型间选择，必须先有一个地方
声明**哪些模型允许给用户选**——否则一次误配就会把云端的贵模型暴露给所有人。

默认 false 是刻意的：升级后所有存量模型都不可选，行为与升级前完全一致，
要开放哪些由管理员在「模型服务」页逐个打开。这与 users.is_admin 用
server_default=false 的理由相同——新能力默认关闭，不因升级而生效。

Revision ID: a7b8c9d0e1f2
Revises: e6f7a8b9c0d1
Create Date: 2026-11-16
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, Sequence[str], None] = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "model_deployments",
        sa.Column(
            "user_selectable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("model_deployments", "user_selectable")
