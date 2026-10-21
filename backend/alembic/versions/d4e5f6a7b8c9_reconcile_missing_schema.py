"""reconcile missing schema: audit_logs / user_api_keys / experiments.owner_id / wiki_pages.links / json->jsonb

Revision ID: d4e5f6a7b8c9
Revises: 820bc90565d8
Create Date: 2026-10-19

背景（docs/design/prod-schema-drift-repair.md）：prod 库 `alembic_version` 被 start-prod
的 stamp 兜底强行标到 head，但实际 DDL 未执行 → 缺 audit_logs / user_api_keys 两表
（二者本就无迁移）、缺 experiments.owner_id / wiki_pages.links 两列，且 experiment 表
沿用 baseline 的 `sa.JSON` 而模型为 JSONB。

本迁移为**幂等「对账」**：仅补齐缺失对象，对已是正确状态的库（如 dev、或全新库经本迁移
之后再跑）为 no-op；同时它也是「纯迁移构建的库」补齐 audit_logs/user_api_keys 的正解。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = '820bc90565d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(conn, table: str) -> bool:
    return conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name=:t"
        ),
        {"t": table},
    ).first() is not None


def _column_exists(conn, table: str, column: str) -> bool:
    return conn.execute(
        sa.text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=:t AND column_name=:c"
        ),
        {"t": table, "c": column},
    ).first() is not None


def _column_is_json(conn, table: str, column: str) -> bool:
    """当前列类型是否为 PG `json`（区别于 `jsonb`，data_type 大小写敏感）。"""
    row = conn.execute(
        sa.text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=:t AND column_name=:c"
        ),
        {"t": table, "c": column},
    ).first()
    return row is not None and str(row[0]).lower() == "json"


def upgrade() -> None:
    """补齐缺失对象（幂等：已存在则跳过）。"""
    bind = op.get_bind()

    # 1) audit_logs（模型 src/models/audit_log.py；此前无迁移）
    if not _table_exists(bind, "audit_logs"):
        op.create_table(
            "audit_logs",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=64), nullable=True),
            sa.Column("username", sa.String(length=128), nullable=True),
            sa.Column("action", sa.String(length=64), nullable=False),
            sa.Column("resource_type", sa.String(length=32), nullable=True),
            sa.Column("resource_id", sa.String(length=64), nullable=True),
            sa.Column("detail", JSONB(), nullable=True),
            sa.Column("ip_address", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_audit_logs_user_id", "audit_logs", ["user_id"], unique=False)
        op.create_index("ix_audit_logs_action", "audit_logs", ["action"], unique=False)
        op.create_index("ix_audit_logs_resource_type", "audit_logs", ["resource_type"], unique=False)
        op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"], unique=False)

    # 2) user_api_keys（模型 src/models/user_api_key.py；此前无迁移）
    if not _table_exists(bind, "user_api_keys"):
        op.create_table(
            "user_api_keys",
            sa.Column("id", sa.UUID(), nullable=False),
            sa.Column("user_id", sa.UUID(), nullable=False),
            sa.Column("name", sa.String(length=64), nullable=False),
            sa.Column("key_hash", sa.String(length=64), nullable=False),
            sa.Column("key_prefix", sa.String(length=12), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_user_api_keys_user_id", "user_api_keys", ["user_id"], unique=False)
        op.create_index("ix_user_api_keys_key_hash", "user_api_keys", ["key_hash"], unique=True)

    # 3) experiments.owner_id（迁移 820bc90565d8 应做而未做）
    if not _column_exists(bind, "experiments", "owner_id"):
        op.add_column(
            "experiments",
            sa.Column("owner_id", sa.VARCHAR(), server_default="", nullable=False),
        )
        op.alter_column(
            "experiments", "owner_id",
            existing_type=sa.VARCHAR(), nullable=False, server_default=None,
        )

    # 4) wiki_pages.links（迁移 b7d2c94a1f30 应做而未做）
    if not _column_exists(bind, "wiki_pages", "links"):
        op.add_column(
            "wiki_pages",
            sa.Column("links", JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        )
        op.alter_column(
            "wiki_pages", "links",
            existing_type=JSONB(), nullable=False, server_default=None,
        )

    # 5) experiment 表 JSON→JSONB 类型对齐（baseline 用 sa.JSON，模型为 JSONB）
    for table, column in (
        ("experiments", "variants"),
        ("experiments", "metrics"),
        ("experiment_variants", "config"),
        ("experiment_results", "analysis_data"),
    ):
        if _column_is_json(bind, table, column):
            # USING 子句必需：空/NULL 行也需可转换
            op.execute(
                sa.text(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE jsonb USING {column}::jsonb")
            )


def downgrade() -> None:
    """应急回退：仅移除本迁移新增的对象。

    类型 jsonb→json 不回退（避免二次风险）；生产回滚以 pg_dump 恢复为主。
    """
    bind = op.get_bind()
    if _column_exists(bind, "wiki_pages", "links"):
        op.drop_column("wiki_pages", "links")
    if _column_exists(bind, "experiments", "owner_id"):
        op.drop_column("experiments", "owner_id")
    if _table_exists(bind, "user_api_keys"):
        op.drop_table("user_api_keys")
    if _table_exists(bind, "audit_logs"):
        op.drop_table("audit_logs")
