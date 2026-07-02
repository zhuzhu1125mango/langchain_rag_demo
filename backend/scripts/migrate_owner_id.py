"""为现有数据补充 owner_id 的迁移脚本。

在引入认证与对象级权限后，历史资源的 owner_id 字段为空或缺失。
本脚本将以下表的 owner_id（或 Session 的 user_id）统一设置为 'default'，
确保现有数据在启用 auth 后仍可被默认用户访问。

用法：
    poetry run python scripts/migrate_owner_id.py
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import update, text
from src.database import async_engine, Base
from src.models import Session as SessionModel, KnowledgeBase, Document, Feedback


async def migrate():
    async with async_engine.begin() as conn:
        # 列不存在时先添加列，再填充默认值
        tables_columns = [
            ("sessions", "user_id", "VARCHAR", "'default'"),
            ("knowledge_bases", "owner_id", "VARCHAR", "'default'"),
            ("documents", "owner_id", "VARCHAR", "'default'"),
            ("feedbacks", "owner_id", "VARCHAR", "'default'"),
        ]

        for table, column, column_type, default_value in tables_columns:
            try:
                result = await conn.execute(
                    text(
                        f"SELECT 1 FROM information_schema.columns "
                        f"WHERE table_name='{table}' AND column_name='{column}'"
                    )
                )
                if not result.scalar():
                    await conn.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
                    )
                    print(f"已添加 {table}.{column}")

                await conn.execute(
                    text(
                        f"UPDATE {table} SET {column} = {default_value} "
                        f"WHERE {column} IS NULL OR {column} = ''"
                    )
                )
                print(f"已更新 {table}.{column} 为默认值")
            except Exception as e:
                print(f"迁移 {table}.{column} 失败: {e}")

    print("owner_id 迁移完成")


if __name__ == "__main__":
    asyncio.run(migrate())
