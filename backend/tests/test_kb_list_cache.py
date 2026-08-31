"""知识库列表 Redis 缓存测试。"""

import pytest

# 依赖真实 PostgreSQL（TestClient 触发 app lifespan + DB 读写；Redis 已 mock），默认跳过
pytestmark = pytest.mark.integration
import uuid


@pytest.fixture
def client(integration_client):
    """委托进程级共享 TestClient（见 conftest.integration_client）。"""
    return integration_client


@pytest.fixture
def mock_cache(monkeypatch):
    """Mock CacheService，避免测试依赖真实 Redis。"""
    class FakeCache:
        def __init__(self):
            self.store = {}
            self.deleted_patterns = []

        async def get(self, key):
            value = self.store.get(key)
            # 模拟 Redis 返回的 JSON 字符串反序列化后的结构
            if isinstance(value, dict):
                return value
            return value

        async def set(self, key, value, expire=None):
            self.store[key] = value
            return True

        async def clear_pattern(self, pattern):
            self.deleted_patterns.append(pattern)
            for k in list(self.store.keys()):
                if k.startswith(pattern.rstrip("*")):
                    self.store.pop(k, None)

    fake = FakeCache()

    async def fake_get_instance():
        return fake

    monkeypatch.setattr("src.api.knowledge_base.CacheService.get_instance", fake_get_instance)
    return fake


class TestKnowledgeBaseListCache:
    """测试知识库列表缓存命中与失效。"""

    def test_list_uses_cache_on_second_request(self, client, mock_cache):
        """第二次请求应命中缓存并返回相同结果。"""
        name = f"缓存测试知识库-{uuid.uuid4().hex[:8]}"
        client.post("/api/knowledge_bases/", json={"name": name})

        response1 = client.get("/api/knowledge_bases/?page=1&page_size=10")
        assert response1.status_code == 200
        data1 = response1.json()

        # 清空内存中缓存，但保留 mock_cache.store 中的值
        response2 = client.get("/api/knowledge_bases/?page=1&page_size=10")
        assert response2.status_code == 200
        assert response2.json() == data1

        # 验证缓存被写入
        assert any("kb:list:" in key for key in mock_cache.store)

    def test_create_invalidates_list_cache(self, client, mock_cache):
        """创建知识库后应清除列表缓存。"""
        client.get("/api/knowledge_bases/?page=1&page_size=10")

        name = f"失效测试知识库-{uuid.uuid4().hex[:8]}"
        response = client.post("/api/knowledge_bases/", json={"name": name})
        assert response.status_code == 200

        assert any("kb:list:" in pattern for pattern in mock_cache.deleted_patterns)

    def test_update_invalidates_list_cache(self, client, mock_cache):
        """更新知识库后应清除列表缓存。"""
        name = f"更新失效测试-{uuid.uuid4().hex[:8]}"
        kb_id = client.post("/api/knowledge_bases/", json={"name": name}).json()["id"]
        client.get("/api/knowledge_bases/?page=1&page_size=10")

        response = client.put(
            f"/api/knowledge_bases/{kb_id}",
            json={"description": "updated"},
        )
        assert response.status_code == 200

        assert any("kb:list:" in pattern for pattern in mock_cache.deleted_patterns)

    def test_delete_invalidates_list_cache(self, client, mock_cache):
        """删除知识库后应清除列表缓存。"""
        name = f"删除失效测试-{uuid.uuid4().hex[:8]}"
        kb_id = client.post("/api/knowledge_bases/", json={"name": name}).json()["id"]
        client.get("/api/knowledge_bases/?page=1&page_size=10")

        response = client.delete(f"/api/knowledge_bases/{kb_id}")
        assert response.status_code == 200

        assert any("kb:list:" in pattern for pattern in mock_cache.deleted_patterns)

    def test_batch_delete_invalidates_list_cache(self, client, mock_cache, monkeypatch):
        """批量删除知识库后应清除列表缓存。"""

        class FakeVectorStoreManager:
            async def delete_by_kb_ids(self, kb_ids):  # noqa: ANN001
                pass

        async def fake_get_instance():
            return FakeVectorStoreManager()

        monkeypatch.setattr(
            "src.api.knowledge_base.VectorStoreManager.get_instance",
            fake_get_instance,
        )

        name = f"批量删除失效测试-{uuid.uuid4().hex[:8]}"
        kb_id = client.post("/api/knowledge_bases/", json={"name": name}).json()["id"]
        client.get("/api/knowledge_bases/?page=1&page_size=10")

        response = client.post(
            "/api/knowledge_bases/batch-delete",
            json={"ids": [kb_id]},
        )
        assert response.status_code == 200

        assert any("kb:list:" in pattern for pattern in mock_cache.deleted_patterns)
