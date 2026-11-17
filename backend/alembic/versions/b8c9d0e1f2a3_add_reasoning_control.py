"""add model_deployments.reasoning_control（思考是否可关）

为什么需要这一列：`capabilities` 里的 `reasoning` 只说明「这个模型会思考」，
没说「思考能不能关」。两者是不同的事实，而且经常不一致——实测 LM Studio 上的
qwen3-4b 无论发 `reasoning_effort` / `chat_template_kwargs.enable_thinking` /
`enable_thinking` / `think` 都照样思考（reasoning_tokens 290→274，属采样波动）。

界面上「深度思考开关」只在**思考可关**时才有意义：
- 模型不会思考 → 开关没有意义
- 模型总会思考、关不掉 → 开关也没有意义（点了也不生效，等于骗人）

所以把「可控性」独立声明出来，默认 `auto` 按协议推断：
- `ollama`：有 `think` 参数，判为可关
- 其它：判为关不掉（不猜「某个兼容端点也许支持」）

默认 auto 是刻意的：升级不改变现有行为，也不需要回填；管理员在确认端点真的
支持关思考后，把它改成 `switchable` 才会开始发送关闭参数。

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-11-17
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, Sequence[str], None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "model_deployments",
        sa.Column(
            "reasoning_control",
            sa.String(length=16),
            nullable=False,
            server_default="auto",
        ),
    )


def downgrade() -> None:
    op.drop_column("model_deployments", "reasoning_control")
