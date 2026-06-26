"""
文档表字段迁移脚本
为 documents 表添加自动分析相关字段

使用方法:
    cd backend && python scripts/migrate_document_fields.py
"""
import asyncio
import asyncpg
from src.config import settings


async def migrate():
    conn = await asyncpg.connect(
        host=settings.database.POSTGRES_HOST,
        port=settings.database.POSTGRES_PORT,
        user=settings.database.POSTGRES_USER,
        password=settings.database.POSTGRES_PASSWORD,
        database=settings.database.POSTGRES_DB
    )

    columns = [
        ("document_type", "VARCHAR DEFAULT ''"),
        ("document_type_label", "VARCHAR DEFAULT ''"),
        ("domain", "VARCHAR DEFAULT ''"),
        ("domain_label", "VARCHAR DEFAULT ''"),
        ("topics", "JSON DEFAULT '[]'"),
        ("summary", "VARCHAR DEFAULT ''"),
        ("quality_score", "INTEGER DEFAULT 0"),
        ("quality_grade", "VARCHAR DEFAULT ''"),
        ("quality_details", "JSON DEFAULT '{}'"),
    ]

    for col_name, col_type in columns:
        try:
            # 检查列是否已存在
            result = await conn.fetchval(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'documents' AND column_name = $1
                """,
                col_name
            )
            if result:
                print(f"列 {col_name} 已存在，跳过")
                continue

            await conn.execute(f'ALTER TABLE documents ADD COLUMN {col_name} {col_type}')
            print(f"成功添加列: {col_name}")
        except Exception as e:
            print(f"添加列 {col_name} 失败: {e}")

    await conn.close()
    print("迁移完成")


if __name__ == "__main__":
    asyncio.run(migrate())
