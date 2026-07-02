"""旧 Milvus 集合迁移脚本。

阶段一升级：为已有集合增加 sparse_embedding 字段以支持 BM25 混合检索。
由于 Milvus 不支持向已存在集合新增向量字段，本脚本通过以下步骤完成迁移：

1. 读取旧集合全部数据（id, kb_id, document_id, content, source, chunk_index 及已有 embedding）。
2. 创建新集合（schema 与升级后的 MilvusService 一致，包含 dense + sparse 双字段）。
3. 使用 OllamaEmbeddings 重新生成 dense embedding；使用 BM25EmbeddingFunction 生成 sparse embedding。
4. 批量插入新集合并创建索引。
5. 可选删除旧集合（默认保留为 backup，需手动删除）。

运行方式：
    cd backend
    poetry run python scripts/migrate_hybrid_index.py

环境要求：
    - Milvus 服务已启动并可连接。
    - Ollama 服务已启动并包含配置的 embedding 模型。
    - 已安装 pymilvus-model 依赖。
"""

import asyncio
import os
import sys
import logging
from typing import Any, Dict, List

# 将 backend 目录加入路径，以便导入 src 模块
backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, backend_dir)

from pymilvus import MilvusClient, DataType
from langchain_ollama import OllamaEmbeddings
from tqdm.asyncio import tqdm_asyncio

from src.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migrate_hybrid_index")

# 依赖检查
try:
    from pymilvus.model.sparse import BM25EmbeddingFunction
    from pymilvus.model.sparse.bm25.tokenizers import build_default_analyzer
except ImportError:
    logger.error("未安装 pymilvus-model，请先执行: poetry add pymilvus-model")
    sys.exit(1)


OLD_COLLECTION = settings.milvus.MILVUS_COLLECTION_NAME
NEW_COLLECTION = f"{OLD_COLLECTION}_hybrid"
BACKUP_COLLECTION = f"{OLD_COLLECTION}_backup"
BATCH_SIZE = 64


def _build_sparse_embeddings(bm25_ef: Any, texts: List[str]) -> List[Dict[int, float]]:
    """将 BM25EmbeddingFunction 输出转换为 Milvus sparse vector 字典。"""
    sparse_matrix = bm25_ef.encode_documents(texts)
    result = []
    for row in sparse_matrix:
        if hasattr(row, "toarray"):
            arr = row.toarray().flatten()
        else:
            arr = row.flatten() if hasattr(row, "flatten") else row
        vec = {int(idx): float(val) for idx, val in enumerate(arr) if val != 0}
        result.append(vec)
    return result


def _build_schema(client: MilvusClient) -> Any:
    """构建新集合 schema。"""
    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("id", DataType.VARCHAR, max_length=64, is_primary=True)
    schema.add_field("kb_id", DataType.VARCHAR, max_length=64, default_value="")
    schema.add_field("document_id", DataType.VARCHAR, max_length=64)
    schema.add_field("content", DataType.VARCHAR, max_length=65535, enable_analyzer=True)
    schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=settings.model.EMBEDDING_DIMENSION)
    schema.add_field("sparse_embedding", DataType.SPARSE_FLOAT_VECTOR)
    schema.add_field("source", DataType.VARCHAR, max_length=512)
    schema.add_field("chunk_index", DataType.INT64)
    return schema


def _create_indexes(client: MilvusClient, collection_name: str):
    """为新集合创建 dense 与 sparse 索引。"""
    dense_index = client.prepare_index_params(
        field_name="embedding",
        metric_type="IP",
        index_type="HNSW",
        params={
            "M": settings.milvus.MILVUS_HNSW_M,
            "efConstruction": settings.milvus.MILVUS_EF_CONSTRUCTION,
        },
        index_name=settings.milvus.MILVUS_INDEX_NAME,
    )
    client.create_index(collection_name=collection_name, index_params=dense_index)

    sparse_index = client.prepare_index_params(
        field_name="sparse_embedding",
        metric_type="IP",
        index_type="SPARSE_INVERTED_INDEX",
        params={"drop_ratio_build": 0.2},
        index_name=f"{settings.milvus.MILVUS_INDEX_NAME}_sparse",
    )
    client.create_index(collection_name=collection_name, index_params=sparse_index)


async def _migrate():
    client = MilvusClient(
        uri=f"http://{settings.milvus.MILVUS_HOST}:{settings.milvus.MILVUS_PORT}",
        db_name=settings.milvus.MILVUS_DATABASE,
    )

    if not client.has_collection(OLD_COLLECTION):
        logger.info(f"旧集合 {OLD_COLLECTION} 不存在，无需迁移")
        return

    # 1. 读取旧集合全部数据
    logger.info(f"开始读取旧集合 {OLD_COLLECTION} 的数据...")
    client.load_collection(OLD_COLLECTION)
    total = client.query(OLD_COLLECTION, filter="", output_fields=["count(*)"])[0]["count(*)"]
    logger.info(f"旧集合共有 {total} 条记录")

    if total == 0:
        logger.info("旧集合为空，直接创建新集合并退出")
        if client.has_collection(NEW_COLLECTION):
            client.drop_collection(NEW_COLLECTION)
        schema = _build_schema(client)
        client.create_collection(NEW_COLLECTION, schema=schema)
        _create_indexes(client, NEW_COLLECTION)
        return

    # 分批查询，避免一次性加载过多数据
    all_records = []
    batch_size = 1000
    for offset in range(0, total, batch_size):
        records = client.query(
            OLD_COLLECTION,
            filter="",
            output_fields=["id", "kb_id", "document_id", "content", "source", "chunk_index"],
            limit=batch_size,
            offset=offset,
        )
        all_records.extend(records)

    logger.info(f"共读取 {len(all_records)} 条记录，开始生成 embedding...")

    # 2. 初始化 embedding 模型与 BM25
    embeddings = OllamaEmbeddings(model=settings.model.EMBEDDING_MODEL_NAME)
    analyzer = build_default_analyzer(language="zh")
    bm25_ef = BM25EmbeddingFunction(analyzer)
    corpus = [r["content"] for r in all_records]
    bm25_ef.fit(corpus)

    # 3. 创建新集合
    if client.has_collection(NEW_COLLECTION):
        logger.warning(f"新集合 {NEW_COLLECTION} 已存在，将删除重建")
        client.drop_collection(NEW_COLLECTION)
    schema = _build_schema(client)
    client.create_collection(NEW_COLLECTION, schema=schema)

    # 4. 分批生成 embedding 并插入
    new_data = []
    for i in tqdm_asyncio(range(0, len(all_records), BATCH_SIZE), desc="迁移进度"):
        batch = all_records[i : i + BATCH_SIZE]
        texts = [r["content"] for r in batch]

        dense_embeddings = await embeddings.aembed_documents(texts)
        sparse_embeddings = _build_sparse_embeddings(bm25_ef, texts)

        for record, dense_vec, sparse_vec in zip(batch, dense_embeddings, sparse_embeddings):
            new_data.append({
                "id": record["id"],
                "kb_id": record.get("kb_id") or "",
                "document_id": record["document_id"],
                "content": record["content"],
                "embedding": dense_vec,
                "sparse_embedding": sparse_vec,
                "source": record.get("source", ""),
                "chunk_index": record.get("chunk_index", 0),
            })

    logger.info(f"正在插入 {len(new_data)} 条记录到新集合 {NEW_COLLECTION}...")
    client.insert(NEW_COLLECTION, data=new_data)
    client.flush(NEW_COLLECTION)

    # 5. 创建索引
    _create_indexes(client, NEW_COLLECTION)
    logger.info("新集合索引创建完成")

    # 6. 重命名集合：旧集合备份，新集合替换为正式名称
    logger.info("执行集合替换...")
    if client.has_collection(BACKUP_COLLECTION):
        client.drop_collection(BACKUP_COLLECTION)
    client.rename_collection(OLD_COLLECTION, BACKUP_COLLECTION)
    client.rename_collection(NEW_COLLECTION, OLD_COLLECTION)

    logger.info(
        f"迁移完成！旧集合已重命名为 {BACKUP_COLLECTION}，"
        f"新集合已重命名为 {OLD_COLLECTION}。"
        f"确认无误后可手动删除 {BACKUP_COLLECTION}。"
    )


if __name__ == "__main__":
    asyncio.run(_migrate())
