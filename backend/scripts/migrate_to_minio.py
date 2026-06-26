"""
本地文件迁移到 MinIO 脚本

将 documents 表中指向本地文件系统的文档迁移到 MinIO 对象存储。

使用方法:
    cd backend && python scripts/migrate_to_minio.py
"""

import os
import sys
import asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from src.database import async_session
from src.models import Document
from src.services.minio_service import MinioService
from src.config import DATA_DIR


async def migrate_files():
    minio_service = MinioService()

    async with async_session() as db:
        result = await db.execute(select(Document))
        docs = result.scalars().all()

        migrated_count = 0
        skipped_count = 0
        failed_count = 0

        for doc in docs:
            if doc.file_path.startswith("minio://"):
                print("[OK] 已迁移: {}".format(doc.filename))
                skipped_count += 1
                continue

            if not os.path.exists(doc.file_path):
                print("[ERR] 文件不存在: {}".format(doc.file_path))
                failed_count += 1
                continue

            try:
                with open(doc.file_path, "rb") as f:
                    file_key = "documents/{}/{}".format(doc.id, doc.filename)
                    minio_service._client.put_object(
                        "documents",
                        file_key,
                        f,
                        os.path.getsize(doc.file_path)
                    )

                doc.file_path = "minio://documents/{}".format(file_key)
                await db.commit()

                local_path = doc.file_path.replace("minio://documents/", "{}/".format(DATA_DIR))
                if os.path.exists(local_path):
                    os.remove(local_path)

                print("[OK] 迁移完成: {}".format(doc.filename))
                migrated_count += 1
            except Exception as e:
                print("[ERR] 迁移失败: {}, 错误: {}".format(doc.filename, str(e)))
                failed_count += 1

        print("\n迁移完成！")
        print("  已迁移: {}".format(migrated_count))
        print("  已跳过: {}".format(skipped_count))
        print("  失败: {}".format(failed_count))


if __name__ == "__main__":
    print("开始迁移文件到 MinIO...")
    asyncio.run(migrate_files())
