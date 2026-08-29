"""向量存储管理器。

对 MilvusService 进行二次封装，将原始检索结果转换为 LangChain Document 对象，
供 RAG 链统一使用。
"""

from langchain_core.documents import Document
from src.services.milvus_service import MilvusService
from src.utils.async_singleton import AsyncSingleton


class VectorStoreManager(AsyncSingleton["VectorStoreManager"]):
    """向量存储管理器，负责 Document 与 Milvus 之间的转换。"""

    def __init__(self):
        self.milvus_service = None

    async def _async_init(self):
        """异步初始化 MilvusService 连接。"""
        self.milvus_service = await MilvusService.get_instance()

    async def create_vector_store(self, documents, kb_id=""):
        """首次创建向量存储并插入文档（语义同 add_documents）。"""
        await self.milvus_service.insert_embeddings(documents, kb_id)
        return self.milvus_service

    async def add_documents(self, documents, kb_id=""):
        """向指定知识库追加文档切片。"""
        await self.milvus_service.insert_embeddings(documents, kb_id)

    async def search(self, query, k=3, document_ids=None, kb_ids=None):
        """检索与查询最相关的文档切片，并包装为 LangChain Document。

        阶段一升级：默认走混合检索（dense + BM25 + RRF + rerank），
        当混合检索未启用或失败时自动回退到纯 dense 检索。

        Args:
            query: 用户查询文本。
            k: 返回结果数量上限。
            document_ids: 可选，限定文档范围。
            kb_ids: 可选，限定知识库范围。

        Returns:
            list[Document]: 包含元数据的 LangChain Document 列表。
        """
        try:
            results = await self.milvus_service.search_hybrid(
                query, k=k, document_ids=document_ids, kb_ids=kb_ids
            )
        except Exception:
            results = await self.milvus_service.search(
                query, k=k, document_ids=document_ids, kb_ids=kb_ids
            )
        return self._results_to_documents(results)

    async def search_dense(self, query, k=3, document_ids=None, kb_ids=None):
        """纯 dense 向量检索入口（兼容旧逻辑）。"""
        results = await self.milvus_service.search_dense(
            query, k=k, document_ids=document_ids, kb_ids=kb_ids
        )
        return self._results_to_documents(results)

    async def search_hybrid(self, query, k=3, document_ids=None, kb_ids=None):
        """混合检索入口，返回经 RRF 融合与 Cross-Encoder 重排序后的 Document。"""
        results = await self.milvus_service.search_hybrid(
            query, k=k, document_ids=document_ids, kb_ids=kb_ids
        )
        return self._results_to_documents(results)

    def _results_to_documents(self, results):
        """将 Milvus 检索结果统一转换为 LangChain Document。"""
        docs = []
        for result in results:
            doc = Document(
                page_content=result["content"],
                metadata={
                    "kb_id": result.get("kb_id", ""),
                    "document_id": result.get("document_id", ""),
                    "source": result.get("source", ""),
                    "chunk_index": result.get("chunk_index", 0),
                    "score": result.get("rerank_score", result.get("rrf_score", result.get("score", 0.0))),
                    "dense_score": result.get("dense_score", 0.0),
                    "sparse_score": result.get("sparse_score", 0.0),
                    "rrf_score": result.get("rrf_score", 0.0),
                    "rerank_score": result.get("rerank_score", 0.0),
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

    async def delete_by_kb_ids(self, kb_ids):
        """按多个知识库 ID 批量删除向量数据，并统一 flush 一次。"""
        await self.milvus_service.delete_by_kb_ids(kb_ids)

    async def flush(self):
        """显式触发 Milvus flush。"""
        await self.milvus_service.flush()

    async def count(self):
        """返回全部切片数量。"""
        return await self.milvus_service.count()

    async def count_by_kb(self, kb_id):
        """返回指定知识库的切片数量。"""
        return await self.milvus_service.count_by_kb(kb_id)
