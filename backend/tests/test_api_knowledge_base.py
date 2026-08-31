"""
知识库API单元测试
"""

import pytest

# 依赖真实 PostgreSQL（TestClient 触发 app lifespan + DB 读写），默认跳过
pytestmark = pytest.mark.integration
import uuid
import asyncio


@pytest.fixture
def client(integration_client):
    """委托进程级共享 TestClient（见 conftest.integration_client）。"""
    return integration_client


@pytest.fixture
def mock_kb_cleanup(monkeypatch):
    """Mock 批量删除知识库时的 MinIO 与向量清理，避免测试受外部服务延迟影响。"""
    class FakeVectorStoreManager:
        def __init__(self):
            self.delete_by_kb_ids_calls = []

        async def delete_by_kb_ids(self, kb_ids):  # noqa: ANN001
            self.delete_by_kb_ids_calls.append(kb_ids)

    fake_instance = FakeVectorStoreManager()

    async def fake_get_instance():
        return fake_instance

    monkeypatch.setattr("src.api.knowledge_base.VectorStoreManager.get_instance", fake_get_instance)

    class FakeMinioService:
        def delete_file(self, file_path):  # noqa: ANN001
            pass

    monkeypatch.setattr("src.services.minio_service.MinioService", FakeMinioService)

    return fake_instance


class TestKnowledgeBaseAPI:
    """知识库API测试类"""

    def test_create_knowledge_base(self, client):
        """测试创建知识库"""
        response = client.post(
            "/api/knowledge_bases/",
            json={"name": f"测试知识库-{uuid.uuid4().hex[:8]}", "description": "测试描述"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["description"] == "测试描述"
        assert "id" in data

    def test_create_duplicate_knowledge_base(self, client):
        """测试创建重复名称的知识库"""
        name = f"重复知识库-{uuid.uuid4().hex[:8]}"
        client.post(
            "/api/knowledge_bases/",
            json={"name": name, "description": "测试"}
        )
        response = client.post(
            "/api/knowledge_bases/",
            json={"name": name, "description": "测试"}
        )
        assert response.status_code == 400

    def test_list_knowledge_bases(self, client):
        """测试获取知识库列表"""
        response = client.get("/api/knowledge_bases/")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)
        assert "items" in data
        assert "total" in data

    def test_get_knowledge_base(self, client):
        """测试获取单个知识库"""
        name = f"获取测试知识库-{uuid.uuid4().hex[:8]}"
        create_response = client.post(
            "/api/knowledge_bases/",
            json={"name": name}
        )
        kb_id = create_response.json()["id"]

        response = client.get(f"/api/knowledge_bases/{kb_id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == kb_id
        assert data["name"] == name

    def test_get_nonexistent_knowledge_base(self, client):
        """测试获取不存在的知识库"""
        fake_id = str(uuid.uuid4())
        response = client.get(f"/api/knowledge_bases/{fake_id}")
        assert response.status_code == 404

    def test_update_knowledge_base(self, client):
        """测试更新知识库"""
        name = f"更新测试知识库-{uuid.uuid4().hex[:8]}"
        create_response = client.post(
            "/api/knowledge_bases/",
            json={"name": name}
        )
        kb_id = create_response.json()["id"]

        response = client.put(
            f"/api/knowledge_bases/{kb_id}",
            json={"name": f"更新后的知识库-{uuid.uuid4().hex[:8]}", "description": "更新后的描述"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["description"] == "更新后的描述"

    def test_delete_knowledge_base(self, client):
        """测试删除知识库"""
        name = f"删除测试知识库-{uuid.uuid4().hex[:8]}"
        create_response = client.post(
            "/api/knowledge_bases/",
            json={"name": name}
        )
        kb_id = create_response.json()["id"]

        response = client.delete(f"/api/knowledge_bases/{kb_id}")
        assert response.status_code == 200

        get_response = client.get(f"/api/knowledge_bases/{kb_id}")
        assert get_response.status_code == 404

    def test_set_default_knowledge_base(self, client):
        """测试设置默认知识库"""
        name = f"默认测试知识库-{uuid.uuid4().hex[:8]}"
        create_response = client.post(
            "/api/knowledge_bases/",
            json={"name": name}
        )
        kb_id = create_response.json()["id"]

        response = client.post(f"/api/knowledge_bases/{kb_id}/set_default")
        assert response.status_code == 200

        default_response = client.get("/api/knowledge_bases/default")
        assert default_response.status_code == 200
        assert default_response.json()["id"] == kb_id

    def test_get_default_knowledge_base(self, client):
        """测试获取默认知识库"""
        response = client.get("/api/knowledge_bases/default")
        assert response.status_code == 200
        assert response.json()["is_default"] is True

    def test_batch_delete_knowledge_bases(self, client, mock_kb_cleanup):
        """测试批量删除知识库"""
        name1 = f"批量删除知识库A-{uuid.uuid4().hex[:8]}"
        name2 = f"批量删除知识库B-{uuid.uuid4().hex[:8]}"
        id1 = client.post("/api/knowledge_bases/", json={"name": name1}).json()["id"]
        id2 = client.post("/api/knowledge_bases/", json={"name": name2}).json()["id"]

        response = client.post(
            "/api/knowledge_bases/batch-delete",
            json={"ids": [id1, id2]},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["deleted_count"] == 2
        assert data["skipped_ids"] == []

        assert client.get(f"/api/knowledge_bases/{id1}").status_code == 404
        assert client.get(f"/api/knowledge_bases/{id2}").status_code == 404

        # 验证批量删除合并为一个 delete_by_kb_ids 调用，避免多次 flush
        assert len(mock_kb_cleanup.delete_by_kb_ids_calls) == 1
        assert sorted(mock_kb_cleanup.delete_by_kb_ids_calls[0]) == sorted([id1, id2])

    def test_batch_delete_knowledge_bases_with_nonexistent(self, client, mock_kb_cleanup):
        """测试批量删除包含不存在的知识库ID时跳过"""
        name = f"批量删除部分-{uuid.uuid4().hex[:8]}"
        kb_id = client.post("/api/knowledge_bases/", json={"name": name}).json()["id"]
        fake_id = str(uuid.uuid4())

        response = client.post(
            "/api/knowledge_bases/batch-delete",
            json={"ids": [kb_id, fake_id]},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["deleted_count"] == 1
        assert data["skipped_ids"] == [fake_id]

        # 验证仅对真实存在的知识库执行一次批量向量删除
        assert len(mock_kb_cleanup.delete_by_kb_ids_calls) == 1
        assert mock_kb_cleanup.delete_by_kb_ids_calls[0] == [kb_id]

    def test_batch_delete_empty_list(self, client):
        """测试批量删除空列表"""
        response = client.post("/api/knowledge_bases/batch-delete", json={"ids": []})
        assert response.status_code == 200
        data = response.json()
        assert data["deleted_count"] == 0
        assert data["skipped_ids"] == []

    def test_batch_delete_exceeds_max_limit(self, client):
        """单次批量删除超过最大限制应返回 400"""
        ids = [str(uuid.uuid4()) for _ in range(1001)]
        response = client.post("/api/knowledge_bases/batch-delete", json={"ids": ids})
        assert response.status_code == 400
        assert "1000" in response.json()["detail"]

    def test_batch_delete_last_knowledge_base(self, client, mock_kb_cleanup):
        """测试批量删除最后一个知识库被拦截"""
        list_response = client.get("/api/knowledge_bases/")
        existing = list_response.json()["items"]
        if len(existing) != 1:
            pytest.skip("当前测试数据不处于仅剩一个知识库的状态，无法可靠构造该场景")

        kb_id = existing[0]["id"]
        response = client.post("/api/knowledge_bases/batch-delete", json={"ids": [kb_id]})
        assert response.status_code == 400
        assert "最后一个" in response.json()["detail"]

        # 被拦截时不应调用向量删除
        assert len(mock_kb_cleanup.delete_by_kb_ids_calls) == 0

    def test_batch_delete_transfers_default(self, client, mock_kb_cleanup):
        """测试批量删除默认知识库时默认标记转移"""
        name_default = f"默认转移源-{uuid.uuid4().hex[:8]}"
        name_other = f"默认转移目标-{uuid.uuid4().hex[:8]}"
        default_id = client.post("/api/knowledge_bases/", json={"name": name_default}).json()["id"]
        other_id = client.post("/api/knowledge_bases/", json={"name": name_other}).json()["id"]
        client.post(f"/api/knowledge_bases/{default_id}/set_default")

        response = client.post(
            "/api/knowledge_bases/batch-delete",
            json={"ids": [default_id]},
        )
        assert response.status_code == 200

        other_response = client.get(f"/api/knowledge_bases/{other_id}")
        assert other_response.status_code == 200
        assert other_response.json()["is_default"] is True

        assert len(mock_kb_cleanup.delete_by_kb_ids_calls) == 1
        assert mock_kb_cleanup.delete_by_kb_ids_calls[0] == [default_id]
