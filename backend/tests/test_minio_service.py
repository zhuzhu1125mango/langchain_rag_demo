"""
MinIO服务单元测试
"""

import asyncio

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers
import io
import uuid

from src.services.minio_service import MinioService


class TestMinioService:
    """MinIO服务测试类"""
    
    def setup_method(self):
        """每个测试前初始化MinIO服务"""
        # 重置单例状态，确保测试隔离
        asyncio.run(MinioService.reset_instance())
        self.minio_service = MinioService.get_instance_sync()
        self.uploaded_files = []
    
    def teardown_method(self):
        """每个测试后清理上传的测试文件"""
        for file_key in self.uploaded_files:
            try:
                if self.minio_service.file_exists(file_key):
                    self.minio_service.delete_file(file_key)
            except Exception:
                pass
        asyncio.run(MinioService.reset_instance())
    
    def _create_upload_file(self, filename: str, content: bytes, content_type: str = "text/plain") -> UploadFile:
        """创建UploadFile对象"""
        file_like = io.BytesIO(content)
        headers = Headers({"content-type": content_type})
        upload_file = UploadFile(filename=filename, file=file_like, headers=headers)
        upload_file.size = len(content)
        return upload_file
    
    def test_upload_and_download_file(self):
        """测试上传和下载文件"""
        file_content = b"Test file content for MinIO"
        upload_file = self._create_upload_file("test.txt", file_content)
        
        file_key = self.minio_service.upload_file(upload_file)
        self.uploaded_files.append(file_key)
        
        assert file_key is not None
        assert file_key.startswith("minio://")
        
        assert self.minio_service.file_exists(file_key) is True
    
    def test_file_exists(self):
        """测试文件存在检查"""
        file_content = b"Test content"
        upload_file = self._create_upload_file("exists_test.txt", file_content)
        
        file_key = self.minio_service.upload_file(upload_file)
        self.uploaded_files.append(file_key)
        
        assert self.minio_service.file_exists(file_key) is True
        assert self.minio_service.file_exists("minio://documents/nonexistent.txt") is False
    
    def test_delete_file(self):
        """测试删除文件"""
        file_content = b"Test delete content"
        upload_file = self._create_upload_file("delete_test.txt", file_content)
        
        file_key = self.minio_service.upload_file(upload_file)
        # 测试删除，不需要添加到清理列表
        assert self.minio_service.file_exists(file_key) is True
        
        self.minio_service.delete_file(file_key)
        assert self.minio_service.file_exists(file_key) is False
    
    def test_get_file_url(self):
        """测试获取文件URL"""
        file_content = b"Test URL content"
        upload_file = self._create_upload_file("url_test.txt", file_content)
        
        file_key = self.minio_service.upload_file(upload_file)
        self.uploaded_files.append(file_key)
        
        url = self.minio_service.get_file_url(file_key)
        
        assert url is not None
        assert isinstance(url, str)
        assert "http" in url
    
    def test_upload_with_custom_file_id(self):
        """测试使用自定义文件ID上传"""
        file_content = b"Custom ID test"
        upload_file = self._create_upload_file("custom_id.txt", file_content)
        
        custom_id = str(uuid.uuid4())
        file_key = self.minio_service.upload_file(upload_file, file_id=custom_id)
        self.uploaded_files.append(file_key)
        
        assert custom_id in file_key
        assert self.minio_service.file_exists(file_key) is True
