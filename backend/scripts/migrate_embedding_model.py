"""Embedding 模型维度迁移脚本。

当切换 Embedding 模型导致向量维度变化时（例如 nomic-embed-text 768 维 → bge-m3 1024 维），
Milvus 不支持直接修改已有集合的向量字段维度。本脚本删除旧集合并按当前配置重建空集合。

由于维度变化后旧 embedding 已不可用，文档需要重新上传；本脚本不会保留旧数据。

运行方式：
    cd backend
    uv run python scripts/migrate_embedding_model.py [--backup]

参数：
    --backup: 迁移前将旧集合重命名为 {collection}_backup，默认直接删除。

环境要求：
    - Milvus 服务已启动并可连接。
"""

import argparse
import asyncio
import logging
import os
import sys

backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, backend_dir)

from pymilvus import AsyncMilvusClient

from src.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migrate_embedding_model")


async def _migrate(backup: bool = False):
    collection_name = settings.milvus.MILVUS_COLLECTION_NAME
    backup_name = f"{collection_name}_backup"

    client = AsyncMilvusClient(
        uri=f"http://{settings.milvus.MILVUS_HOST}:{settings.milvus.MILVUS_PORT}",
        db_name=settings.milvus.MILVUS_DATABASE,
    )

    has_collection = await client.has_collection(collection_name)
    if not has_collection:
        logger.info(f"集合 {collection_name} 不存在，无需迁移")
        return

    if backup:
        if await client.has_collection(backup_name):
            logger.warning(f"备份集合 {backup_name} 已存在，将删除旧备份")
            await client.drop_collection(backup_name)
        logger.info(f"将旧集合重命名为 {backup_name}")
        await client.rename_collection(collection_name, backup_name)
    else:
        logger.info(f"删除旧集合 {collection_name}")
        await client.drop_collection(collection_name)

    logger.info(
        f"按 embedding 维度 {settings.model.EMBEDDING_DIMENSION} 重建集合 {collection_name}..."
    )

    # 复用 MilvusService 的 schema/index 构建逻辑，保证一致性
    from src.services.milvus_service import MilvusService

    service = MilvusService()
    service.client = client
    schema = await service._build_schema()
    await client.create_collection(collection_name=collection_name, schema=schema)
    await service._create_indexes()
    await client.load_collection(collection_name)

    logger.info("集合重建完成。请重新上传文档以生成新的 embedding。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Embedding 模型维度迁移脚本")
    parser.add_argument("--backup", action="store_true", help="迁移前备份旧集合")
    args = parser.parse_args()
    asyncio.run(_migrate(backup=args.backup))
