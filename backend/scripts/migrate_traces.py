"""数据库迁移脚本 - request_traces 追加可观测性字段（P1-2）。

为 request_traces 表新增：
- stages: 分阶段耗时明细（JSON 数组，每项含 name/status/latency_ms/detail）
- token_usage: token 用量（JSON 对象，含 prompt_tokens/completion_tokens/estimated）

使用方法:
    cd backend && uv run python scripts/migrate_traces.py

幂等：列已存在时跳过，不删除任何数据。
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import async_engine
from sqlalchemy import text


async def add_trace_columns():
    """为 request_traces 追加 stages / token_usage 列（幂等）。

    新建列使用 JSONB；若列已存在但为旧 JSON 类型，则原位升级为 JSONB
    （与 ORM 模型及 jsonb 查询函数保持一致）。
    """
    print("=== request_traces 追加可观测性字段 ===")

    async with async_engine.begin() as conn:
        await conn.execute(
            text("ALTER TABLE request_traces ADD COLUMN IF NOT EXISTS stages JSONB")
        )
        await conn.execute(
            text("ALTER TABLE request_traces ADD COLUMN IF NOT EXISTS token_usage JSONB")
        )

        # 旧环境可能已用 JSON 类型创建过列，原位升级为 JSONB
        for column in ("stages", "token_usage"):
            result = await conn.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name='request_traces' AND column_name=:col"
                ),
                {"col": column},
            )
            if result.scalar() == "json":
                await conn.execute(
                    text(
                        f"ALTER TABLE request_traces ALTER COLUMN {column} "
                        f"TYPE JSONB USING {column}::jsonb"
                    )
                )
                print(f" request_traces.{column}: JSON -> JSONB")

    print(" request_traces - stages / token_usage 列已就绪")
    print("=== 迁移完成 ===")


if __name__ == "__main__":
    asyncio.run(add_trace_columns())
