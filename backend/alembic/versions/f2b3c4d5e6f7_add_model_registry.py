"""add model provider/deployment/route registry (P2)

Revision ID: f2b3c4d5e6f7
Revises: e5f6a7b8c9d0
Create Date: 2026-10-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f2b3c4d5e6f7"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "model_providers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("provider_type", sa.String(length=32), nullable=False),
        sa.Column("base_url", sa.String(length=512), nullable=True),
        sa.Column("api_key_ciphertext", sa.Text(), nullable=True),
        sa.Column("api_key_env", sa.String(length=128), nullable=True),
        sa.Column("api_key_required", sa.Boolean(), nullable=False),
        sa.Column("headers", sa.JSON(), nullable=False),
        sa.Column("timeout_s", sa.Float(), nullable=False),
        sa.Column("external", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "model_deployments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("provider_id", sa.String(length=36), nullable=False),
        sa.Column("model_name", sa.String(length=256), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("context_window", sa.Integer(), nullable=False),
        sa.Column("max_output_tokens", sa.Integer(), nullable=False),
        sa.Column("embedding_dimension", sa.Integer(), nullable=False),
        sa.Column("temperature", sa.Float(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["provider_id"], ["model_providers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_model_deployments_provider_id", "model_deployments", ["provider_id"], unique=False)

    op.create_table(
        "model_routes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task", sa.String(length=64), nullable=False),
        sa.Column("primary_model_id", sa.String(length=36), nullable=False),
        sa.Column("fallback_model_ids", sa.JSON(), nullable=False),
        sa.Column("reasoning", sa.String(length=16), nullable=False),
        sa.Column("temperature", sa.Float(), nullable=True),
        sa.Column("max_output_tokens", sa.Integer(), nullable=True),
        sa.Column("timeout_s", sa.Float(), nullable=True),
        sa.Column("allow_external_fallback", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["primary_model_id"], ["model_deployments.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task"),
    )


def downgrade() -> None:
    op.drop_table("model_routes")
    op.drop_index("ix_model_deployments_provider_id", table_name="model_deployments")
    op.drop_table("model_deployments")
    op.drop_table("model_providers")
