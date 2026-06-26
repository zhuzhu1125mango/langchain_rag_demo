"""
会话管理API单元测试
"""

import pytest
from fastapi.testclient import TestClient
from src.main import app
import uuid

client = TestClient(app)


class TestSessionAPI:
    """会话管理API测试类"""
    
    def test_create_session(self):
        """测试创建会话"""
        response = client.post(
            "/api/sessions/",
            json={"title": "测试会话"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert "id" in data
        assert data["title"] == "测试会话"
    
    def test_list_sessions(self):
        """测试获取会话列表"""
        response = client.get("/api/sessions/")
        
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
    
    def test_get_session(self):
        """测试获取单个会话"""
        create_response = client.post(
            "/api/sessions/",
            json={"title": "获取测试会话"}
        )
        session_id = create_response.json()["id"]
        
        response = client.get(f"/api/sessions/{session_id}")
        
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == session_id
        assert data["title"] == "获取测试会话"
    
    def test_get_nonexistent_session(self):
        """测试获取不存在的会话"""
        fake_id = str(uuid.uuid4())
        response = client.get(f"/api/sessions/{fake_id}")
        
        assert response.status_code == 404
    
    def test_update_session(self):
        """测试更新会话"""
        create_response = client.post(
            "/api/sessions/",
            json={"title": "更新测试会话"}
        )
        session_id = create_response.json()["id"]
        
        response = client.put(
            f"/api/sessions/{session_id}",
            json={"title": "更新后的会话"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "更新后的会话"
    
    def test_delete_session(self):
        """测试删除会话"""
        create_response = client.post(
            "/api/sessions/",
            json={"title": "删除测试会话"}
        )
        session_id = create_response.json()["id"]
        
        response = client.delete(f"/api/sessions/{session_id}")
        
        assert response.status_code == 200
        
        get_response = client.get(f"/api/sessions/{session_id}")
        assert get_response.status_code == 404
    
    def test_session_with_knowledge_bases(self):
        """测试会话关联知识库"""
        kb_response = client.post(
            "/api/knowledge_bases/",
            json={"name": "会话测试KB"}
        )
        kb_id = kb_response.json()["id"]
        
        response = client.post(
            "/api/sessions/",
            json={"title": "KB关联会话", "kb_ids": [kb_id]}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert kb_id in data["kb_ids"]