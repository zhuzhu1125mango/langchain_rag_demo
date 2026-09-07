"""context_builder 单元测试。

覆盖去重、预算管理、Lost in the Middle 重排序、统一编号、多源融合等场景。
"""

import sys

import pytest
from langchain_core.documents import Document

from src.services.context_builder import ContextBuilder, estimate_token_count


class TestEstimateTokenCount:
    """token 估算测试。"""

    @pytest.fixture(autouse=True)
    def _force_fallback(self, monkeypatch):
        """屏蔽 tiktoken，固定测试字符启发式估算分支（tiktoken 是否安装因环境而异）。"""
        monkeypatch.setitem(sys.modules, "tiktoken", None)

    def test_chinese_text(self):
        """中文文本按字符估算。"""
        assert estimate_token_count("你好世界") == 4

    def test_english_text(self):
        """英文文本按词估算。"""
        assert estimate_token_count("hello world") == 2

    def test_mixed_text(self):
        """中英文混合估算。"""
        assert estimate_token_count("hello 世界") == 3


class TestContextBuilder:
    """上下文构建器核心测试。"""

    def test_empty_sources(self):
        """无来源时返回空。"""
        builder = ContextBuilder()
        context, sources = builder.build_context()
        assert context == ""
        assert sources == []

    def test_kb_docs_numbering(self):
        """知识库文档应被统一编号。"""
        docs = [
            Document(page_content="内容A", metadata={"filename": "a.txt", "score": 0.9}),
            Document(page_content="内容B", metadata={"filename": "b.txt", "score": 0.8}),
        ]
        builder = ContextBuilder()
        context, sources = builder.build_context(kb_docs=docs)

        assert "[1]" in context
        assert "[2]" in context
        assert len(sources) == 2
        assert sources[0]["source_index"] == 1
        assert sources[1]["source_index"] == 2

    def test_deduplication(self):
        """重复内容应被去重。"""
        docs = [
            Document(page_content="相同内容", metadata={"score": 0.9}),
            Document(page_content="相同内容", metadata={"score": 0.7}),
        ]
        builder = ContextBuilder()
        context, sources = builder.build_context(kb_docs=docs)

        assert len(sources) == 1
        # 保留分数更高的
        assert sources[0]["score"] == 0.9

    def test_budget_truncation(self):
        """超出预算时应截断低分片段。"""
        # 使用带空格的文本，确保 token 估算 > 1
        docs = [
            Document(page_content=" ".join(["word"] * 50), metadata={"score": 0.9}),
            Document(page_content=" ".join(["other"] * 50), metadata={"score": 0.1}),
        ]
        # 预算只够第一条（50 tokens + 10 开销）
        builder = ContextBuilder(token_budget=60, reserve_tokens=0)
        context, sources = builder.build_context(kb_docs=docs)

        assert len(sources) == 1
        assert "word" in sources[0]["content"]

    def test_lost_in_the_middle_reorder(self):
        """高相关片段应位于开头和结尾。"""
        docs = [
            Document(page_content=f"内容{i}", metadata={"score": float(score)})
            for i, score in enumerate([0.9, 0.8, 0.7, 0.6, 0.5])
        ]
        builder = ContextBuilder()
        context, sources = builder.build_context(kb_docs=docs)

        # 输入按分数降序 [0.9, 0.8, 0.7, 0.6, 0.5]
        # Lost in the Middle 后顺序应为 [0.9, 0.7, 0.5, 0.6, 0.8]
        scores = [s["score"] for s in sources]
        assert scores[0] == 0.9
        assert scores[-1] == 0.8
        assert scores[2] == 0.5

    def test_multi_source_build(self):
        """混合来源应统一编号。"""
        docs = [Document(page_content="KB内容", metadata={"score": 0.9})]
        web_sources = [{"title": "网页", "content": "Web内容", "url": "http://example.com", "score": 0.8}]
        builder = ContextBuilder()
        context, sources = builder.build_context(kb_docs=docs, web_sources=web_sources)

        assert len(sources) == 2
        assert sources[0]["source_type"] == "kb"
        assert sources[1]["source_type"] == "web"
        assert "[1]" in context and "[2]" in context

    def test_web_source_header(self):
        """网页来源应包含标题和 URL。"""
        web_sources = [{"title": "标题", "content": "内容", "url": "http://a.com", "score": 0.8}]
        builder = ContextBuilder()
        context, _ = builder.build_context(web_sources=web_sources)

        assert "标题" in context
        assert "http://a.com" in context

    def test_min_score_filter(self):
        """低于阈值的结果应被过滤。"""
        docs = [
            Document(page_content="高分", metadata={"score": 0.9}),
            Document(page_content="低分", metadata={"score": 0.1}),
        ]
        builder = ContextBuilder(min_relevance_score=0.5)
        _, sources = builder.build_context(kb_docs=docs)

        assert len(sources) == 1
        assert sources[0]["content"] == "高分"
