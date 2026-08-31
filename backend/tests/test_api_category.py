"""
分类管理API单元测试
"""

import pytest

# 依赖真实 PostgreSQL（TestClient 触发 app lifespan + DB 读写），默认跳过
pytestmark = pytest.mark.integration
import uuid


@pytest.fixture
def client(integration_client):
    """委托进程级共享 TestClient（见 conftest.integration_client）。"""
    return integration_client


class TestCategoryAPI:
    """分类管理API测试类"""

    def test_create_category(self, client):
        """测试创建分类"""
        response = client.post(
            "/api/categories/",
            json={"name": f"测试分类-{uuid.uuid4().hex[:8]}", "description": "测试描述"}
        )

        assert response.status_code == 200
        data = response.json()
        assert "id" in data
        assert data["name"].startswith("测试分类-")

    def test_create_duplicate_category(self, client):
        """测试创建重复名称的分类"""
        name = f"重复分类-{uuid.uuid4().hex[:8]}"
        client.post(
            "/api/categories/",
            json={"name": name}
        )

        response = client.post(
            "/api/categories/",
            json={"name": name}
        )

        assert response.status_code == 400

    def test_list_categories(self, client):
        """测试获取分类列表"""
        response = client.get("/api/categories/")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    def test_get_category(self, client):
        """测试获取单个分类"""
        name = f"获取测试分类-{uuid.uuid4().hex[:8]}"
        create_response = client.post(
            "/api/categories/",
            json={"name": name}
        )
        category_id = create_response.json()["id"]

        response = client.get(f"/api/categories/{category_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == category_id
        assert data["name"] == name

    def test_get_nonexistent_category(self, client):
        """测试获取不存在的分类"""
        fake_id = str(uuid.uuid4())
        response = client.get(f"/api/categories/{fake_id}")

        assert response.status_code == 404

    def test_update_category(self, client):
        """测试更新分类"""
        name = f"更新测试分类-{uuid.uuid4().hex[:8]}"
        create_response = client.post(
            "/api/categories/",
            json={"name": name}
        )
        category_id = create_response.json()["id"]

        response = client.put(
            f"/api/categories/{category_id}",
            json={"name": f"更新后的分类-{uuid.uuid4().hex[:8]}", "description": "更新后的描述"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["message"] == "更新成功"

    def test_delete_category(self, client):
        """测试删除分类"""
        name = f"删除测试分类-{uuid.uuid4().hex[:8]}"
        create_response = client.post(
            "/api/categories/",
            json={"name": name}
        )
        category_id = create_response.json()["id"]

        response = client.delete(f"/api/categories/{category_id}")

        assert response.status_code == 200

        get_response = client.get(f"/api/categories/{category_id}")
        assert get_response.status_code == 404
