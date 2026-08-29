"""
向量存储单元测试 - Milvus版本
"""

import asyncio

import pytest
from langchain_core.documents import Document

from src.services.milvus_service import MilvusService
from src.services.vector_store import VectorStoreManager


class TestVectorStore:
    """向量存储测试类"""

    @pytest.fixture(autouse=True)
    async def reset_singleton(self):
        """每个测试前后重置单例状态。

        必须同时重置 VectorStoreManager 与其依赖的 MilvusService，
        否则 AsyncMilvusClient 会绑定到上一个测试的事件循环，导致
        后续测试出现 'Event loop is closed'。
        """
        await VectorStoreManager.reset_instance()
        await MilvusService.reset_instance()
        yield
        await VectorStoreManager.reset_instance()
        await MilvusService.reset_instance()

    @pytest.mark.asyncio
    async def test_create_vector_store(self):
        """测试创建向量存储"""
        docs = [
            Document(page_content="这是第一个文档", metadata={"source": "test1"}),
            Document(page_content="这是第二个文档", metadata={"source": "test2"})
        ]

        manager = await VectorStoreManager.get_instance()
        await manager.create_vector_store(docs)

        assert manager.milvus_service is not None

    @pytest.mark.asyncio
    async def test_search_documents(self):
        """测试文档检索功能"""
        docs = [
            Document(page_content="人工智能是计算机科学的一个分支", metadata={"source": "ai"}),
            Document(page_content="机器学习是人工智能的核心技术", metadata={"source": "ml"}),
            Document(page_content="Python是一种流行的编程语言", metadata={"source": "python"})
        ]

        manager = await VectorStoreManager.get_instance()
        await manager.create_vector_store(docs)

        results = await manager.search("人工智能")
        assert len(results) > 0
        assert "人工智能" in results[0].page_content

    @pytest.mark.asyncio
    async def test_add_documents(self):
        """测试添加文档"""
        docs1 = [Document(page_content="第一个文档", metadata={"source": "1"})]
        docs2 = [Document(page_content="第二个文档", metadata={"source": "2"})]

        manager = await VectorStoreManager.get_instance()
        await manager.create_vector_store(docs1)
        await manager.add_documents(docs2)

        results = await manager.search("第二个文档")
        assert len(results) > 0

    @pytest.mark.asyncio
    async def test_count_documents(self):
        """测试文档计数"""
        docs = [
            Document(page_content="测试文档1", metadata={"source": "test1"}),
            Document(page_content="测试文档2", metadata={"source": "test2"}),
            Document(page_content="测试文档3", metadata={"source": "test3"})
        ]

        manager = await VectorStoreManager.get_instance()
        await manager.create_vector_store(docs)

        count = await manager.count()
        assert count >= 3
