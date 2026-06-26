"""
分类管理API单元测试
"""

import pytest
from fastapi.testclient import TestClient
from src.main import app
import uuid

client = TestClient(app)


class TestCategoryAPI:
    """分类管理API测试类"""
    
    def test_create_category(self):
        """测试创建分类"""
        response = client.post(
            "/api/categories/",
            json={"name": "测试分类", "description": "测试描述"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert "id" in data
        assert data["name"] == "测试分类"
        assert data["description"] == "测试描述"
    
    def test_create_duplicate_category(self):
        """测试创建重复名称的分类"""
        client.post(
            "/api/categories/",
            json={"name": "重复分类"}
        )
        
        response = client.post(
            "/api/categories/",
            json={"name": "重复分类"}
        )
        
        assert response.status_code == 400
    
    def test_list_categories(self):
        """测试获取分类列表"""
        response = client.get("/api/categories/")
        
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
    
    def test_get_category(self):
        """测试获取单个分类"""
        create_response = client.post(
            "/api/categories/",
            json={"name": "获取测试分类"}
        )
        category_id = create_response.json()["id"]
        
        response = client.get(f"/api/categories/{category_id}")
        
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == category_id
        assert data["name"] == "获取测试分类"
    
    def test_get_nonexistent_category(self):
        """测试获取不存在的分类"""
        fake_id = str(uuid.uuid4())
        response = client.get(f"/api/categories/{fake_id}")
        
        assert response.status_code == 404
    
    def test_update_category(self):
        """测试更新分类"""
        create_response = client.post(
            "/api/categories/",
            json={"name": "更新测试分类"}
        )
        category_id = create_response.json()["id"]
        
        response = client.put(
            f"/api/categories/{category_id}",
            json={"name": "更新后的分类", "description": "更新后的描述"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "更新后的分类"
        assert data["description"] == "更新后的描述"
    
    def test_delete_category(self):
        """测试删除分类"""
        create_response = client.post(
            "/api/categories/",
            json={"name": "删除测试分类"}
        )
        category_id = create_response.json()["id"]
        
        response = client.delete(f"/api/categories/{category_id}")
        
        assert response.status_code == 200
        
        get_response = client.get(f"/api/categories/{category_id}")
        assert get_response.status_code == 404