"""
会话管理API单元测试
"""

import pytest

# 依赖真实 PostgreSQL（TestClient 触发 app lifespan + DB 读写），默认跳过
pytestmark = pytest.mark.integration
import uuid


@pytest.fixture
def client(integration_client):
    """委托进程级共享 TestClient（见 conftest.integration_client）。"""
    return integration_client


class TestSessionAPI:
    """会话管理API测试类"""

    def test_create_session(self, client):
        """测试创建会话"""
        response = client.post(
            "/api/sessions/",
            json={"title": "测试会话"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert "id" in data
        assert data["title"] == "测试会话"
    
    def test_list_sessions(self, client):
        """测试获取会话列表"""
        response = client.get("/api/sessions/")
        
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
    
    def test_get_session(self, client):
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
    
    def test_get_nonexistent_session(self, client):
        """测试获取不存在的会话"""
        fake_id = str(uuid.uuid4())
        response = client.get(f"/api/sessions/{fake_id}")
        
        assert response.status_code == 404
    
    def test_update_session(self, client):
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
    
    def test_delete_session(self, client):
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
    
    def test_session_with_knowledge_bases(self, client):
        """测试会话关联知识库"""
        kb_name = f"会话测试KB-{uuid.uuid4().hex[:8]}"
        kb_response = client.post(
            "/api/knowledge_bases/",
            json={"name": kb_name}
        )
        kb_id = kb_response.json()["id"]

        response = client.post(
            "/api/sessions/",
            json={"title": "KB关联会话", "kb_ids": [kb_id]}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert kb_id in data["kb_ids"]

class TestSessionFiltersAndExport:
    """UX-5：历史会话的搜索 / 筛选 / 导出。

    为什么这些是集成测试而不是单测：命中行数只有真库能给。
    条件拼装是否正确（ILIKE / @> / 日期区间）由 `test_session_filters.py` 用 SQL 编译覆盖，
    这里只验「端到端能不能筛出正确的行、导出能不能拿到正确的内容与下载头」。
    """

    @staticmethod
    def _create(client, **payload) -> str:
        """建一个带随机后缀的会话，避免与其它用例的数据互相干扰。"""
        payload.setdefault("title", f"筛选测试-{uuid.uuid4().hex[:8]}")
        response = client.post("/api/sessions/", json=payload)
        assert response.status_code == 200, response.text
        return response.json()["id"]

    def test_search_by_title(self, client):
        tag = uuid.uuid4().hex[:8]
        hit = self._create(client, title=f"弹性打卡-{tag}")
        miss = self._create(client, title=f"报销流程-{tag}")

        response = client.get("/api/sessions/", params={"q": f"弹性打卡-{tag}"})
        assert response.status_code == 200
        ids = [item["id"] for item in response.json()]
        assert hit in ids
        assert miss not in ids

    def test_search_with_wildcard_does_not_match_everything(self, client):
        """`%` 必须当普通字符：否则搜「100%」会把所有会话都搜出来。"""
        tag = uuid.uuid4().hex[:8]
        target = self._create(client, title=f"命中100%-{tag}")
        other = self._create(client, title=f"另一个-{tag}")

        response = client.get("/api/sessions/", params={"q": f"100%-{tag}"})
        ids = [item["id"] for item in response.json()]
        assert target in ids
        assert other not in ids

    def test_search_without_match_returns_empty(self, client):
        response = client.get("/api/sessions/", params={"q": f"绝不存在的词-{uuid.uuid4().hex}"})
        assert response.status_code == 200
        assert response.json() == []

    def test_filter_by_kb(self, client):
        kb_id = str(uuid.uuid4())
        with_kb = self._create(client, title=f"带库-{uuid.uuid4().hex[:8]}", kb_ids=[kb_id])
        without_kb = self._create(client, title=f"不带库-{uuid.uuid4().hex[:8]}", kb_ids=[])

        response = client.get("/api/sessions/", params={"kb_id": kb_id})
        ids = [item["id"] for item in response.json()]
        assert with_kb in ids
        assert without_kb not in ids

    def test_filter_by_future_date_returns_empty(self, client):
        """未来日期必然筛不到：这条同时证明日期过滤真的下推到了 SQL，而不是被忽略。"""
        self._create(client, title=f"今天-{uuid.uuid4().hex[:8]}")
        response = client.get("/api/sessions/", params={"date_from": "2099-01-01"})
        assert response.status_code == 200
        assert response.json() == []

    def test_filter_today_includes_new_session(self, client):
        from datetime import date

        session_id = self._create(client, title=f"今天-{uuid.uuid4().hex[:8]}")
        today = date.today().isoformat()
        response = client.get("/api/sessions/", params={"date_from": today, "date_to": today})
        assert session_id in [item["id"] for item in response.json()]

    def test_invalid_date_returns_422(self, client):
        response = client.get("/api/sessions/", params={"date_from": "不是日期"})
        assert response.status_code == 422

    def test_export_markdown(self, client):
        title = f"导出测试-{uuid.uuid4().hex[:8]}"
        session_id = self._create(client, title=title)

        response = client.get(f"/api/sessions/{session_id}/export")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/markdown")
        disposition = response.headers["content-disposition"]
        assert "attachment" in disposition
        assert "filename*=UTF-8''" in disposition  # 中文标题必须走 RFC 5987
        assert title in response.text
        assert "_该会话还没有消息。_" in response.text

    def test_export_json(self, client):
        session_id = self._create(client, title=f"导出JSON-{uuid.uuid4().hex[:8]}")
        response = client.get(f"/api/sessions/{session_id}/export", params={"format": "json"})
        assert response.status_code == 200
        body = response.json()
        assert body["session"]["id"] == session_id
        assert body["messages"] == []

    def test_export_rejects_unknown_format(self, client):
        session_id = self._create(client)
        response = client.get(f"/api/sessions/{session_id}/export", params={"format": "pdf"})
        assert response.status_code == 422

    def test_export_invalid_id_returns_400(self, client):
        response = client.get("/api/sessions/not-a-uuid/export")
        assert response.status_code == 400

    def test_export_missing_session_returns_404(self, client):
        response = client.get(f"/api/sessions/{uuid.uuid4()}/export")
        assert response.status_code == 404
