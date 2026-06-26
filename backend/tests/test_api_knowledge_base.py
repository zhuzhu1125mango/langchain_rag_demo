"""
知识库API单元测试
"""

import pytest
from fastapi.testclient import TestClient
from src.main import app
from src.models import KnowledgeBase
from src.database import get_db, init_db
from sqlalchemy.orm import Session
import uuid

client = TestClient(app)


@pytest.fixture(scope="module")
def db_session():
    """创建测试数据库会话"""
    pass


class TestKnowledgeBaseAPI:
    """知识库API测试类"""
    
    def test_create_knowledge_base(self):
        """测试创建知识库"""
        response = client.post(
            "/api/knowledge_bases/",
            json={"name": "测试知识库", "description": "测试描述"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "测试知识库"
        assert data["description"] == "测试描述"
        assert "id" in data
    
    def test_create_duplicate_knowledge_base(self):
        """测试创建重复名称的知识库"""
        client.post(
            "/api/knowledge_bases/",
            json={"name": "重复知识库", "description": "测试"}
        )
        response = client.post(
            "/api/knowledge_bases/",
            json={"name": "重复知识库", "description": "测试"}
        )
        assert response.status_code == 400
    
    def test_list_knowledge_bases(self):
        """测试获取知识库列表"""
        response = client.get("/api/knowledge_bases/")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
    
    def test_get_knowledge_base(self):
        """测试获取单个知识库"""
        create_response = client.post(
            "/api/knowledge_bases/",
            json={"name": "获取测试知识库"}
        )
        kb_id = create_response.json()["id"]
        
        response = client.get(f"/api/knowledge_bases/{kb_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == kb_id
        assert data["name"] == "获取测试知识库"
    
    def test_get_nonexistent_knowledge_base(self):
        """测试获取不存在的知识库"""
        fake_id = str(uuid.uuid4())
        response = client.get(f"/api/knowledge_bases/{fake_id}")
        assert response.status_code == 404
    
    def test_update_knowledge_base(self):
        """测试更新知识库"""
        create_response = client.post(
            "/api/knowledge_bases/",
            json={"name": "更新测试知识库"}
        )
        kb_id = create_response.json()["id"]
        
        response = client.put(
            f"/api/knowledge_bases/{kb_id}",
            json={"name": "更新后的知识库", "description": "更新后的描述"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "更新后的知识库"
        assert data["description"] == "更新后的描述"
    
    def test_delete_knowledge_base(self):
        """测试删除知识库"""
        create_response = client.post(
            "/api/knowledge_bases/",
            json={"name": "删除测试知识库"}
        )
        kb_id = create_response.json()["id"]
        
        response = client.delete(f"/api/knowledge_bases/{kb_id}")
        assert response.status_code == 200
        
        get_response = client.get(f"/api/knowledge_bases/{kb_id}")
        assert get_response.status_code == 404
    
    def test_set_default_knowledge_base(self):
        """测试设置默认知识库"""
        create_response = client.post(
            "/api/knowledge_bases/",
            json={"name": "默认测试知识库"}
        )
        kb_id = create_response.json()["id"]
        
        response = client.post(f"/api/knowledge_bases/{kb_id}/set_default")
        assert response.status_code == 200
        
        default_response = client.get("/api/knowledge_bases/default")
        assert default_response.status_code == 200
        assert default_response.json()["id"] == kb_id
    
    def test_get_default_knowledge_base(self):
        """测试获取默认知识库"""
        response = client.get("/api/knowledge_bases/default")
        assert response.status_code == 200
        assert response.json()["is_default"] == True