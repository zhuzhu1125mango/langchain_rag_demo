"""
文档管理API单元测试
"""

import pytest

# 依赖真实 PostgreSQL（TestClient 触发 app lifespan + DB 读写），默认跳过
pytestmark = pytest.mark.integration
import tempfile
import os

# 历史上此处为模块级 `client = TestClient(app)`：无 lifespan 且每次请求使用
# 独立临时事件循环，asyncpg 连接跨循环复用导致 "Event loop is closed"。
# 改为每测试把模块全局 client 指向进程级共享 TestClient（见 conftest.integration_client）。
client = None


@pytest.fixture(autouse=True)
def _shared_client(integration_client):
    global client
    client = integration_client


class TestDocumentAPI:
    """文档API测试类"""
    
    def test_upload_document(self):
        """测试上传文档"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("测试文档内容")
            temp_path = f.name
        
        try:
            with open(temp_path, 'rb') as file:
                response = client.post(
                    "/api/documents/upload",
                    files={"file": ("test.txt", file, "text/plain")}
                )
            assert response.status_code == 200
            data = response.json()
            assert "id" in data
            assert data["message"] == "文件上传成功，共1个文本块"
        finally:
            os.unlink(temp_path)
    
    def test_upload_unsupported_file_type(self):
        """测试上传不支持的文件类型"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.xyz', delete=False) as f:
            f.write("测试内容")
            temp_path = f.name
        
        try:
            with open(temp_path, 'rb') as file:
                response = client.post(
                    "/api/documents/upload",
                    files={"file": ("test.xyz", file, "application/octet-stream")}
                )
            assert response.status_code == 400
            assert "不支持的文件类型" in response.json()["detail"]
        finally:
            os.unlink(temp_path)
    
    def test_list_documents(self):
        """测试获取文档列表"""
        response = client.get("/api/documents/")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
    
    def test_get_document(self):
        """测试获取文档详情"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("测试文档")
            temp_path = f.name
        
        try:
            with open(temp_path, 'rb') as file:
                upload_response = client.post(
                    "/api/documents/upload",
                    files={"file": ("test_doc.txt", file, "text/plain")}
                )
            doc_id = upload_response.json()["id"]
            
            response = client.get(f"/api/documents/{doc_id}")
            assert response.status_code == 200
            data = response.json()
            assert data["filename"] == "test_doc.txt"
        finally:
            os.unlink(temp_path)
    
    def test_get_nonexistent_document(self):
        """测试获取不存在的文档"""
        response = client.get("/api/documents/00000000-0000-0000-0000-000000000000")
        assert response.status_code == 404
    
    def test_update_document(self):
        """测试更新文档"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("测试文档")
            temp_path = f.name
        
        try:
            with open(temp_path, 'rb') as file:
                upload_response = client.post(
                    "/api/documents/upload",
                    files={"file": ("update_test.txt", file, "text/plain")}
                )
            doc_id = upload_response.json()["id"]
            
            response = client.put(
                f"/api/documents/{doc_id}",
                json={"tags": ["测试标签", "重要"]}
            )
            assert response.status_code == 200
            assert response.json()["message"] == "更新成功"
        finally:
            os.unlink(temp_path)
    
    def test_delete_document(self):
        """测试删除文档"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("测试文档")
            temp_path = f.name
        
        try:
            with open(temp_path, 'rb') as file:
                upload_response = client.post(
                    "/api/documents/upload",
                    files={"file": ("delete_test.txt", file, "text/plain")}
                )
            doc_id = upload_response.json()["id"]
            
            response = client.delete(f"/api/documents/{doc_id}")
            assert response.status_code == 200
            
            get_response = client.get(f"/api/documents/{doc_id}")
            assert get_response.status_code == 404
        finally:
            os.unlink(temp_path)
    
    def test_batch_upload_documents(self):
        """测试批量上传文档"""
        files = []
        temp_files = []
        
        try:
            for i in range(3):
                tf = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False)
                tf.write(f"文档{i}内容")
                temp_files.append(tf)
                tf.seek(0)
                files.append(("files", (f"doc{i}.txt", tf.file, "text/plain")))
            
            response = client.post(
                "/api/documents/batch",
                files=files
            )
            assert response.status_code == 200
            data = response.json()
            assert "results" in data
            assert len(data["results"]) == 3
        finally:
            for tf in temp_files:
                tf.close()
                os.unlink(tf.name)
    
    def test_get_chunk_config(self):
        """测试获取文本块配置"""
        response = client.get("/api/documents/chunk-config")
        assert response.status_code == 200
        data = response.json()
        assert "chunk_size" in data
        assert "chunk_overlap" in data
        assert "supported_range" in data
    
    def test_preview_document(self):
        """测试预览文档"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("测试预览内容" * 100)
            temp_path = f.name
        
        try:
            with open(temp_path, 'rb') as file:
                upload_response = client.post(
                    "/api/documents/upload",
                    files={"file": ("preview.txt", file, "text/plain")}
                )
            doc_id = upload_response.json()["id"]
            
            response = client.get(f"/api/documents/{doc_id}/preview")
            assert response.status_code == 200
            data = response.json()
            assert "content" in data
            assert "total_length" in data
        finally:
            os.unlink(temp_path)