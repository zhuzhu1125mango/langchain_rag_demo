"""
RAG问答链单元测试
"""

import pytest
from src.services.rag_chain import RAGChain
from src.services.vector_store import VectorStoreManager
from langchain_core.documents import Document


class TestRAGChain:
    """RAG问答链测试类"""

    async def test_rag_chain_initialization(self):
        """测试RAG链初始化"""
        vector_store = VectorStoreManager()
        rag_chain = RAGChain(vector_store)
        # LLM 在 _async_init() 中异步初始化
        await rag_chain._async_init()

        assert rag_chain.llm is not None
        assert rag_chain.vector_store is not None
        assert rag_chain.retriever is None  # 延迟初始化

    def test_has_vector_store_without_store(self):
        """测试向量库不存在时的判断"""
        vector_store = VectorStoreManager()
        rag_chain = RAGChain(vector_store)

        assert rag_chain._has_vector_store() is False

    @pytest.mark.skip(reason="依赖真实 Milvus 服务，create_vector_store 需要已初始化的 Milvus 连接，无法在单元测试中运行")
    async def test_has_vector_store_with_store(self):
        """测试向量库存在时的判断"""
        vector_store = VectorStoreManager()
        docs = [Document(page_content="测试文档", metadata={"source": "test"})]
        await vector_store.create_vector_store(docs)

        rag_chain = RAGChain(vector_store)
        assert rag_chain._has_vector_store() is True

    async def test_should_use_knowledge_base_without_store(self):
        """测试向量库不存在时不使用知识库"""
        vector_store = VectorStoreManager()
        rag_chain = RAGChain(vector_store)

        result = await rag_chain._should_use_knowledge_base("什么是人工智能？")
        assert result is False

    @pytest.mark.skip(reason="依赖真实 Milvus 服务，create_vector_store 需要已初始化的 Milvus 连接，无法在单元测试中运行")
    async def test_should_use_knowledge_base_greeting(self):
        """测试问候语不使用知识库"""
        vector_store = VectorStoreManager()
        docs = [Document(page_content="测试文档", metadata={"source": "test"})]
        await vector_store.create_vector_store(docs)

        rag_chain = RAGChain(vector_store)

        result = await rag_chain._should_use_knowledge_base("你好")
        assert result is False

        result = await rag_chain._should_use_knowledge_base("Hi")
        assert result is False

    async def test_retrieve_documents_empty(self):
        """测试向量库为空时的检索"""
        vector_store = VectorStoreManager()
        rag_chain = RAGChain(vector_store)

        results = await rag_chain._retrieve_documents("测试问题")
        assert len(results) == 0

    @pytest.mark.skip(reason="检索器已改为始终返回 None（VectorStoreManager 使用 Milvus 直接搜索，不再提供 LangChain retriever），且 create_vector_store 依赖真实 Milvus 服务")
    async def test_get_retriever_lazy_initialization(self):
        """测试检索器延迟初始化"""
        vector_store = VectorStoreManager()
        docs = [Document(page_content="测试文档", metadata={"source": "test"})]
        await vector_store.create_vector_store(docs)

        rag_chain = RAGChain(vector_store)
        assert rag_chain.retriever is None

        retriever = rag_chain._get_retriever()
        assert retriever is not None
        assert rag_chain.retriever is not None
