"""
文档处理器单元测试
"""

import os
import tempfile
import pytest
from src.services.document_processor import (
    load_document,
    split_documents,
    process_document,
    SUPPORTED_EXTENSIONS,
    ChunkingStrategy,
    ChunkingFactory,
)


class TestDocumentProcessor:
    """文档处理器测试类"""
    
    def test_load_txt_document(self):
        """测试加载TXT文档"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as f:
            f.write("测试文档内容\n这是一个测试文件。")
            temp_path = f.name
        
        try:
            docs = load_document(temp_path)
            assert len(docs) > 0
            assert "测试文档内容" in docs[0].page_content
        finally:
            os.unlink(temp_path)
    
    def test_split_documents(self):
        """测试文档切分功能"""
        from langchain_core.documents import Document
        from src.config import settings
        
        docs = [Document(page_content="a" * 1000)]
        chunks = split_documents(docs)
        
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk.page_content) <= settings.processing.CHUNK_SIZE

    def test_process_document(self):
        """测试完整文档处理流程"""
        from src.config import settings

        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as f:
            f.write("测试内容\n" * 100)
            temp_path = f.name
        
        try:
            chunks = process_document(temp_path)
            assert len(chunks) > 0
            assert all(len(ch.page_content) <= settings.processing.CHUNK_SIZE for ch in chunks)
        finally:
            os.unlink(temp_path)
    
    def test_unsupported_file_type(self):
        """测试不支持的文件类型"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.xyz', delete=False) as f:
            f.write("test")
            temp_path = f.name
        
        try:
            with pytest.raises(ValueError):
                load_document(temp_path)
        finally:
            os.unlink(temp_path)
    
    def test_supported_extensions(self):
        """测试支持的文件扩展名列表"""
        assert ".txt" in SUPPORTED_EXTENSIONS
        assert ".pdf" in SUPPORTED_EXTENSIONS
        assert ".docx" in SUPPORTED_EXTENSIONS
        assert ".md" in SUPPORTED_EXTENSIONS


class TestChunkingFactory:
    """分块策略工厂测试"""

    def test_auto_strategy_for_markdown(self):
        """auto 模式应正确识别 markdown 文件"""
        strategy = ChunkingFactory.get_strategy("doc.md")
        assert strategy == ChunkingStrategy.MARKDOWN

    def test_auto_strategy_for_code(self):
        """auto 模式应正确识别代码文件"""
        strategy = ChunkingFactory.get_strategy("script.py")
        assert strategy == ChunkingStrategy.CODE

    def test_auto_strategy_default_recursive(self):
        """未知扩展名应回退到 recursive"""
        strategy = ChunkingFactory.get_strategy("doc.unknown")
        assert strategy == ChunkingStrategy.RECURSIVE

    def test_explicit_strategy_overrides_auto(self):
        """显式策略应覆盖 auto 推断"""
        strategy = ChunkingFactory.get_strategy("doc.md", ChunkingStrategy.RECURSIVE)
        assert strategy == ChunkingStrategy.RECURSIVE

    def test_markdown_split(self):
        """Markdown 应按标题分块"""
        from langchain_core.documents import Document

        content = "# 标题1\n内容1\n## 标题2\n内容2\n# 标题3\n内容3"
        docs = [Document(page_content=content)]
        chunks = ChunkingFactory.split(docs, file_path="test.md", chunk_strategy=ChunkingStrategy.MARKDOWN)

        assert len(chunks) >= 2
        assert all(chunk.metadata.get("doc_type") == "markdown" for chunk in chunks)

    def test_code_split(self):
        """代码文件应使用语言特定分隔符"""
        from langchain_core.documents import Document

        content = "def foo():\n    pass\n\ndef bar():\n    pass"
        docs = [Document(page_content=content)]
        chunks = ChunkingFactory.split(docs, file_path="test.py", chunk_strategy=ChunkingStrategy.CODE)

        assert len(chunks) >= 1
        assert all(chunk.metadata.get("doc_type") == "code" for chunk in chunks)

    def test_process_document_with_strategy(self):
        """process_document 应支持指定分块策略"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False, encoding='utf-8') as f:
            f.write("# 标题\n正文内容\n")
            temp_path = f.name

        try:
            chunks = process_document(temp_path, chunk_strategy=ChunkingStrategy.MARKDOWN)
            assert len(chunks) >= 1
            assert chunks[0].metadata.get("chunk_strategy") == "markdown"
        finally:
            os.unlink(temp_path)

    def test_split_documents_backward_compatible(self):
        """split_documents 默认行为保持向后兼容"""
        from langchain_core.documents import Document
        from src.config import settings

        docs = [Document(page_content="a" * 1000)]
        chunks = split_documents(docs)

        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk.page_content) <= settings.processing.CHUNK_SIZE
            assert chunk.metadata.get("chunk_strategy") == "recursive"