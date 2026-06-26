"""Milvus 向量数据库服务。

负责与 Milvus 交互，提供集合管理、向量插入、相似度检索、按文档/知识库删除等功能。
使用单例模式避免重复初始化连接与 Embedding 模型。
"""

import asyncio
import uuid
from pymilvus import AsyncMilvusClient, DataType
from langchain_ollama import OllamaEmbeddings
from src.config import settings


class MilvusService:
    """Milvus 客户端封装，管理集合生命周期与向量操作。"""

    _instance = None
    _lock = asyncio.Lock()
    _initialized = False

    def __init__(self):
        self.client: AsyncMilvusClient | None = None
        self.embeddings: OllamaEmbeddings | None = None

    async def _async_init(self):
        """异步初始化 Milvus 连接、Embedding 模型并确保集合就绪。"""
        async with MilvusService._lock:
            if MilvusService._initialized:
                return
            self.client = AsyncMilvusClient(
                uri=f"http://{settings.milvus.MILVUS_HOST}:{settings.milvus.MILVUS_PORT}",
                db_name=settings.milvus.MILVUS_DATABASE
            )
            self.embeddings = OllamaEmbeddings(model=settings.model.EMBEDDING_MODEL_NAME)
            await self._ensure_collection()
            MilvusService._initialized = True

    @classmethod
    async def get_instance(cls) -> "MilvusService":
        """获取 MilvusService 单例。"""
        if cls._instance is None:
            cls._instance = cls()
        await cls._instance._async_init()
        return cls._instance

    async def _ensure_collection(self):
        has_collection = await self.client.has_collection(settings.milvus.MILVUS_COLLECTION_NAME)
        if not has_collection:
            schema = AsyncMilvusClient.create_schema(
                auto_id=False,
                enable_dynamic_field=False
            )
            schema.add_field("id", DataType.VARCHAR, max_length=64, is_primary=True)
            schema.add_field("kb_id", DataType.VARCHAR, max_length=64, default_value="")
            schema.add_field("document_id", DataType.VARCHAR, max_length=64)
            schema.add_field("content", DataType.VARCHAR, max_length=65535)
            schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=768)
            schema.add_field("source", DataType.VARCHAR, max_length=512)
            schema.add_field("chunk_index", DataType.INT64)

            await self.client.create_collection(
                collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
                schema=schema
            )

            index_params = self.client.prepare_index_params(
                field_name="embedding",
                metric_type="IP",
                index_type="HNSW",
                params={"M": 8, "efConstruction": 64},
                index_name=settings.milvus.MILVUS_INDEX_NAME
            )
            await self.client.create_index(
                collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
                index_params=index_params
            )
        else:
            await self._add_kb_id_field_if_missing()
            await self._ensure_index()
        await self.client.load_collection(settings.milvus.MILVUS_COLLECTION_NAME)

    async def _add_kb_id_field_if_missing(self):
        """兼容旧集合：若缺少 kb_id 字段则删除重建集合。

        注意：旧数据会丢失，仅用于schema升级时的兼容处理。
        """
        try:
            collection_info = await self.client.describe_collection(settings.milvus.MILVUS_COLLECTION_NAME)
            field_names = [f["name"] for f in collection_info["fields"]]
            if "kb_id" not in field_names:
                await self.client.drop_collection(settings.milvus.MILVUS_COLLECTION_NAME)
                schema = AsyncMilvusClient.create_schema(
                    auto_id=False,
                    enable_dynamic_field=False
                )
                schema.add_field("id", DataType.VARCHAR, max_length=64, is_primary=True)
                schema.add_field("kb_id", DataType.VARCHAR, max_length=64, default_value="")
                schema.add_field("document_id", DataType.VARCHAR, max_length=64)
                schema.add_field("content", DataType.VARCHAR, max_length=65535)
                schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=768)
                schema.add_field("source", DataType.VARCHAR, max_length=512)
                schema.add_field("chunk_index", DataType.INT64)

                await self.client.create_collection(
                    collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
                    schema=schema
                )

                index_params = self.client.prepare_index_params(
                    field_name="embedding",
                    metric_type="IP",
                    index_type="HNSW",
                    params={"M": 8, "efConstruction": 64},
                    index_name=settings.milvus.MILVUS_INDEX_NAME
                )
                await self.client.create_index(
                    collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
                    index_params=index_params
                )
        except Exception:
            pass

    async def _ensure_index(self):
        """确保向量索引存在；若不存在则创建 HNSW 索引。"""
        try:
            indexes = await self.client.list_indexes(settings.milvus.MILVUS_COLLECTION_NAME)
            if settings.milvus.MILVUS_INDEX_NAME not in indexes:
                index_params = self.client.prepare_index_params(
                    field_name="embedding",
                    metric_type="IP",
                    index_type="HNSW",
                    params={"M": 8, "efConstruction": 64},
                    index_name=settings.milvus.MILVUS_INDEX_NAME
                )
                await self.client.create_index(
                    collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
                    index_params=index_params
                )
        except Exception:
            pass

    async def insert_embeddings(self, documents, kb_id=""):
        """将文档切块后的 embedding 批量插入 Milvus。

        Args:
            documents: 待插入的 Document 列表，每个文档需包含 document_id/source/chunk_index 元数据。
            kb_id: 知识库 ID，用于按知识库过滤。

        Returns:
            int: 实际插入的切片数量。
        """
        embeddings_list = await self.embeddings.aembed_documents(
            [doc.page_content for doc in documents]
        )

        data = []
        for doc, embedding in zip(documents, embeddings_list):
            chunk_id = str(uuid.uuid4())
            data.append({
                "id": chunk_id,
                "kb_id": kb_id,
                "document_id": doc.metadata.get("document_id", ""),
                "content": doc.page_content,
                "embedding": embedding,
                "source": doc.metadata.get("source", ""),
                "chunk_index": doc.metadata.get("chunk_index", 0)
            })

        await self.client.insert(
            collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
            data=data
        )
        await self.client.flush(collection_name=settings.milvus.MILVUS_COLLECTION_NAME)
        return len(data)

    async def search(self, query, k=3, document_ids=None, kb_ids=None):
        """基于向量相似度检索相关切片。

        Args:
            query: 用户查询文本。
            k: 返回结果数量上限。
            document_ids: 可选，限定只在这些文档中检索。
            kb_ids: 可选，限定只在这些知识库中检索。

        Returns:
            list[dict]: 包含 content/document_id/kb_id/source/score 等字段的结果列表。
        """
        query_embedding = await self.embeddings.aembed_query(query)

        search_params = {
            "metric_type": "IP",
            "params": {"ef": 64}
        }

        filter_expr = None
        conditions = []

        if kb_ids and len(kb_ids) > 0:
            conditions.append(f"kb_id in [{','.join([f'\"{kb}\"' for kb in kb_ids])}]")

        if document_ids and len(document_ids) > 0:
            conditions.append(f"document_id in [{','.join([f'\"{doc_id}\"' for doc_id in document_ids])}]")

        if conditions:
            filter_expr = " && ".join(conditions)

        results = await self.client.search(
            collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
            data=[query_embedding],
            anns_field="embedding",
            search_params=search_params,
            limit=k,
            filter=filter_expr,
            output_fields=["kb_id", "document_id", "content", "source", "chunk_index"]
        )

        docs = []
        for hit in results[0]:
            doc = {
                "id": hit["id"],
                "kb_id": hit["entity"].get("kb_id"),
                "document_id": hit["entity"].get("document_id"),
                "content": hit["entity"].get("content"),
                "source": hit["entity"].get("source"),
                "chunk_index": hit["entity"].get("chunk_index"),
                "score": hit["distance"]
            }
            docs.append(doc)

        return docs

    async def delete_by_document_id(self, document_id):
        """按文档 ID 删除其所有切片。"""
        expr = f"document_id == '{document_id}'"
        await self.client.delete(
            collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
            filter=expr
        )
        await self.client.flush(collection_name=settings.milvus.MILVUS_COLLECTION_NAME)

    async def delete_by_kb_id(self, kb_id):
        """按知识库 ID 删除其下所有切片。"""
        expr = f"kb_id == '{kb_id}'"
        await self.client.delete(
            collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
            filter=expr
        )
        await self.client.flush(collection_name=settings.milvus.MILVUS_COLLECTION_NAME)

    async def get_document_chunks(self, document_id):
        """获取指定文档的所有切片内容。"""
        expr = f"document_id == '{document_id}'"
        results = await self.client.query(
            collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
            filter=expr,
            output_fields=["id", "kb_id", "content", "chunk_index", "source"]
        )
        return results

    async def count(self):
        """返回集合中切片总数。"""
        results = await self.client.query(
            collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
            filter="",
            output_fields=["count(*)"]
        )
        return results[0]["count(*)"]

    async def count_by_kb(self, kb_id):
        """返回指定知识库下的切片数量。"""
        expr = f"kb_id == '{kb_id}'"
        results = await self.client.query(
            collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
            filter=expr,
            output_fields=["count(*)"]
        )
        return results[0]["count(*)"]

    async def drop_collection(self):
        """删除当前集合（慎用）。"""
        has_collection = await self.client.has_collection(settings.milvus.MILVUS_COLLECTION_NAME)
        if has_collection:
            await self.client.drop_collection(settings.milvus.MILVUS_COLLECTION_NAME)

    async def reconnect(self):
        """重新建立 Milvus 连接并确保集合就绪。"""
        self.client = AsyncMilvusClient(
            uri=f"http://{settings.milvus.MILVUS_HOST}:{settings.milvus.MILVUS_PORT}",
            db_name=settings.milvus.MILVUS_DATABASE
        )
        await self._ensure_collection()
