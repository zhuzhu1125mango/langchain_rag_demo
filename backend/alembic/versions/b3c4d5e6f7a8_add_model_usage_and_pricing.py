"""add model pricing and daily usage persistence (P2.1)

Revision ID: b3c4d5e6f7a8
Revises: f2b3c4d5e6f7
Create Date: 2026-10-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b3c4d5e6f7a8"
down_revision: Union[str, Sequence[str], None] = "f2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("model_deployments", sa.Column("input_cost_per_1k", sa.Float(), nullable=True))
    op.add_column("model_deployments", sa.Column("output_cost_per_1k", sa.Float(), nullable=True))
    op.add_column(
        "model_deployments",
        sa.Column("price_currency", sa.String(length=8), nullable=False, server_default="USD"),
    )

    op.create_table(
        "model_usage_daily",
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("provider_id", sa.String(length=36), nullable=False),
        sa.Column("model_id", sa.String(length=36), nullable=False),
        sa.Column("task", sa.String(length=64), nullable=False),
        sa.Column("cost_currency", sa.String(length=8), nullable=False),
        sa.Column("model_name", sa.String(length=256), nullable=False),
        sa.Column("calls", sa.Integer(), nullable=False),
        sa.Column("errors", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("total_latency_ms", sa.Float(), nullable=False),
        sa.Column("priced_calls", sa.Integer(), nullable=False),
        sa.Column("unpriced_calls", sa.Integer(), nullable=False),
        sa.Column("estimated_cost", sa.Float(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.PrimaryKeyConstraint("usage_date", "provider_id", "model_id", "task", "cost_currency"),
    )
    op.create_index("ix_model_usage_daily_usage_date", "model_usage_daily", ["usage_date"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_model_usage_daily_usage_date", table_name="model_usage_daily")
    op.drop_table("model_usage_daily")
    op.drop_column("model_deployments", "price_currency")
    op.drop_column("model_deployments", "output_cost_per_1k")
    op.drop_column("model_deployments", "input_cost_per_1k")
