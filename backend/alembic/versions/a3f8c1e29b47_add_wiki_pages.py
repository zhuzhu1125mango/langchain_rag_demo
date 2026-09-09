"""新增 wiki_pages 表（P2 LLM-Wiki 编译层 Phase 1）

Revision ID: a3f8c1e29b47
Revises: 27af4193802c
Create Date: 2026-09-07

LLM 编译产出的 Wiki 页面元数据；正文持久化在 MinIO（wiki/{kb_id}/{page_id}.md）。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a3f8c1e29b47'
down_revision: str = '27af4193802c'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'wiki_pages',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('kb_id', sa.String(length=36), nullable=False),
        sa.Column('page_type', sa.String(length=16), nullable=False),
        sa.Column('title', sa.String(length=256), nullable=False),
        sa.Column('content_path', sa.String(length=512), nullable=False),
        sa.Column('source_doc_ids', postgresql.JSONB(astext_type=sa.Text()),
                  server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column('revision', sa.Integer(), server_default=sa.text('1'), nullable=False),
        sa.Column('status', sa.String(length=16), server_default='active', nullable=False),
        sa.Column('owner_id', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('kb_id', 'page_type', 'title',
                            name='uq_wiki_pages_kb_type_title'),
    )
    op.create_index(op.f('ix_wiki_pages_kb_id'), 'wiki_pages', ['kb_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_wiki_pages_kb_id'), table_name='wiki_pages')
    op.drop_table('wiki_pages')
