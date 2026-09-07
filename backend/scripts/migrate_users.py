"""数据库迁移脚本 - 创建用户表（P1-1 JWT 多用户认证）。

创建 users 表：
- users: 用户账号表（id 即资源 owner_id，知识库/文档/会话按其隔离）

使用方法:
    cd backend && uv run python scripts/migrate_users.py

幂等：表已存在时跳过，不删除任何数据。
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import async_engine
from sqlalchemy import text


async def create_users_table():
    """创建用户表（幂等）。"""
    print("=== 创建用户表 ===")

    async with async_engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    username VARCHAR(64) NOT NULL UNIQUE,
                    password_hash VARCHAR(128) NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_users_username ON users (username)")
        )

    print(" users - 用户账号表（username 唯一）")
    print("=== 迁移完成 ===")


if __name__ == "__main__":
    asyncio.run(create_users_table())
