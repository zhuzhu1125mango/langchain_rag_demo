"""wiki_pages 增加 links 交叉链接列（P3 交叉链接检索扩展）

Revision ID: b7d2c94a1f30
Revises: a3f8c1e29b47
Create Date: 2026-09-09

存量页在下次重编译（rebuild）时自然回填，不强制立即迁移重跑。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b7d2c94a1f30'
down_revision: str = 'a3f8c1e29b47'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'wiki_pages',
        sa.Column('links', postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'[]'::jsonb"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column('wiki_pages', 'links')
