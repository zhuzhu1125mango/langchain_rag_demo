"""add users.is_admin for admin identity (P0-2)

管理员身份从「一把全局 ADMIN_KEY」改为「用户表上的 is_admin 标志」。
全局 Key 保留为应急引导通道，但不再是唯一的管理入口。

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-11-02
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, Sequence[str], None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default=false()：存量用户一律非管理员，避免升级即提权。
    # 首个管理员需由 scripts/create_admin.py 显式指定（或注册首个用户时自动获得）。
    op.add_column(
        "users",
        sa.Column(
            "is_admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_index("ix_users_is_admin", "users", ["is_admin"])


def downgrade() -> None:
    op.drop_index("ix_users_is_admin", table_name="users")
    op.drop_column("users", "is_admin")
