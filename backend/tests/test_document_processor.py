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
    SUPPORTED_EXTENSIONS
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