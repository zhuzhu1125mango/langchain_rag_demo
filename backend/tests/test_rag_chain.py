"""
RAG问答链单元测试

被 skip 的用例已通过 stub 替代真实 Milvus 依赖恢复运行：
- create_vector_store 只需 milvus_service 非空即可让 _has_vector_store() 为 True，
  用 stub 对象替代真实连接即可覆盖原有断言。
- 检索器已废弃（VectorStoreManager 使用 Milvus 直接搜索），对应用例改为断言当前契约。
"""

import pytest
from src.services.rag_chain import RAGChain
from src.services.vector_store import VectorStoreManager


class _StubMilvusService:
    """最小化的 MilvusService 替身：仅用于让 _has_vector_store() 判定为存在。"""


def make_vector_store_with_stub():
    """构造已连接（stub）状态的 VectorStoreManager，无需真实 Milvus。"""
    vector_store = VectorStoreManager()
    vector_store.milvus_service = _StubMilvusService()
    return vector_store


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

    async def test_has_vector_store_with_store(self):
        """测试向量库存在时的判断（stub milvus_service 模拟已连接状态）"""
        vector_store = make_vector_store_with_stub()
        rag_chain = RAGChain(vector_store)
        assert rag_chain._has_vector_store() is True

    async def test_should_use_knowledge_base_without_store(self):
        """测试向量库不存在时不使用知识库"""
        vector_store = VectorStoreManager()
        rag_chain = RAGChain(vector_store)

        result = await rag_chain._should_use_knowledge_base("什么是人工智能？")
        assert result is False

    async def test_should_use_knowledge_base_greeting(self, monkeypatch):
        """测试问候语不使用知识库。

        决策由 _make_decision 管道给出；此处置换为返回 False 的桩实现，
        验证问候语场景下 _should_use_knowledge_base 全链路输出 False，
        无需真实 LLM/Milvus。
        """
        vector_store = make_vector_store_with_stub()
        rag_chain = RAGChain(vector_store)

        async def fake_make_decision(question, kb_ids=None, history=None, **kwargs):
            assert question in ("你好", "Hi")  # 决策管道应收到原始问题
            return False

        monkeypatch.setattr(rag_chain, "_make_decision", fake_make_decision)

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

    async def test_get_retriever_deprecated_returns_none(self):
        """测试检索器契约：已废弃，恒返回 None（检索由 _retrieve_documents 直接完成）"""
        vector_store = make_vector_store_with_stub()
        rag_chain = RAGChain(vector_store)

        retriever = rag_chain._get_retriever()
        assert retriever is None
        assert rag_chain.retriever is None


class _FakeDoc:
    """最小 Document 替身（_extract_source_info 只读 page_content/metadata）。"""

    def __init__(self, metadata):
        self.page_content = "片段内容"
        self.metadata = metadata


class TestExtractSourceInfo:
    """P4：来源元数据透传 source_kind（wiki 编译页标识）。"""

    def test_wiki_source_kind_passed_through(self):
        rag_chain = RAGChain(make_vector_store_with_stub())
        docs = [_FakeDoc({"document_id": "page-1", "source": "wiki://页面A", "source_kind": "wiki"})]

        _, metadata = rag_chain._extract_source_info(docs)

        assert metadata[0]["source_kind"] == "wiki"

    def test_raw_source_kind_default(self):
        """metadata 无 source_kind 时兜底 'raw'（联网/工具来源不受影响）。"""
        rag_chain = RAGChain(make_vector_store_with_stub())
        docs = [_FakeDoc({"document_id": "doc-1", "filename": "a.pdf"})]

        _, metadata = rag_chain._extract_source_info(docs)

        assert metadata[0]["source_kind"] == "raw"
