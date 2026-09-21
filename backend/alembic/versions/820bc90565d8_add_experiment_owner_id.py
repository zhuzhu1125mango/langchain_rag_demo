"""add experiments.owner_id for object-level authorization

Revision ID: <REVISION>
Revises: 8cb6e5b283ec
Create Date: 2026-09-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '820bc90565d8'
down_revision: Union[str, Sequence[str], None] = '8cb6e5b283ec'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 存量行回填 ''（空串，与 require_owner 的"owner 为空即遗留放行"约定一致）
    op.add_column('experiments', sa.Column('owner_id', sa.VARCHAR(), server_default='', nullable=False))
    # 移除 server_default，确保新插入的实验必须显式写入 owner_id
    op.alter_column('experiments', 'owner_id',
                    existing_type=sa.VARCHAR(), nullable=False, server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('experiments', 'owner_id')