"""add model index profile for embedding migration (P3)

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-10-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, Sequence[str], None] = "b3c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "model_index_profile",
        sa.Column("collection_name", sa.String(length=128), nullable=False),
        sa.Column("profile_id", sa.String(length=512), nullable=False),
        sa.Column("provider_id", sa.String(length=36), nullable=False),
        sa.Column("model_id", sa.String(length=36), nullable=False),
        sa.Column("model_name", sa.String(length=256), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("chunk_count", sa.BigInteger(), nullable=False),
        sa.Column("previous_profile_id", sa.String(length=512), nullable=True),
        sa.Column("previous_model_id", sa.String(length=36), nullable=True),
        sa.Column("previous_dimension", sa.Integer(), nullable=True),
        sa.Column("backup_collection", sa.String(length=128), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.PrimaryKeyConstraint("collection_name"),
    )


def downgrade() -> None:
    op.drop_table("model_index_profile")
