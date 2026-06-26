"""向量存储管理器。

对 MilvusService 进行二次封装，将原始检索结果转换为 LangChain Document 对象，
供 RAG 链统一使用。
"""

import asyncio
from langchain_core.documents import Document
from src.services.milvus_service import MilvusService


class VectorStoreManager:
    """向量存储管理器，负责 Document 与 Milvus 之间的转换。"""

    _instance = None
    _lock = asyncio.Lock()
    _initialized = False

    def __init__(self):
        self.milvus_service = None

    async def _async_init(self):
        """异步初始化 MilvusService 连接。"""
        async with VectorStoreManager._lock:
            if VectorStoreManager._initialized:
                return
            self.milvus_service = await MilvusService.get_instance()
            VectorStoreManager._initialized = True

    @classmethod
    async def get_instance(cls) -> "VectorStoreManager":
        """获取 VectorStoreManager 单例。"""
        if cls._instance is None:
            cls._instance = cls()
        await cls._instance._async_init()
        return cls._instance

    async def create_vector_store(self, documents, kb_id=""):
        """首次创建向量存储并插入文档（语义同 add_documents）。"""
        await self.milvus_service.insert_embeddings(documents, kb_id)
        return self.milvus_service

    async def save_vector_store(self):
        """Milvus 实时落盘，此方法保留以兼容旧接口，无需额外操作。"""
        pass

    async def load_vector_store(self):
        """Milvus 由 MilvusService 自动加载，此方法保留以兼容旧接口。"""
        return True

    async def add_documents(self, documents, kb_id=""):
        """向指定知识库追加文档切片。"""
        await self.milvus_service.insert_embeddings(documents, kb_id)

    async def search(self, query, k=3, document_ids=None, kb_ids=None):
        """检索与查询最相关的文档切片，并包装为 LangChain Document。

        Args:
            query: 用户查询文本。
            k: 返回结果数量上限。
            document_ids: 可选，限定文档范围。
            kb_ids: 可选，限定知识库范围。

        Returns:
            list[Document]: 包含元数据的 LangChain Document 列表。
        """
        results = await self.milvus_service.search(query, k=k, document_ids=document_ids, kb_ids=kb_ids)
        docs = []
        for result in results:
            doc = Document(
                page_content=result["content"],
                metadata={
                    "kb_id": result.get("kb_id", ""),
                    "document_id": result["document_id"],
                    "source": result["source"],
                    "chunk_index": result["chunk_index"],
                    "score": result["score"]
                }
            )
            docs.append(doc)
        return docs

    async def delete_by_document_id(self, document_id):
        """按文档 ID 删除向量数据。"""
        await self.milvus_service.delete_by_document_id(document_id)

    async def delete_by_kb_id(self, kb_id):
        """按知识库 ID 删除向量数据。"""
        await self.milvus_service.delete_by_kb_id(kb_id)

    async def count(self):
        """返回全部切片数量。"""
        return await self.milvus_service.count()

    async def count_by_kb(self, kb_id):
        """返回指定知识库的切片数量。"""
        return await self.milvus_service.count_by_kb(kb_id)
