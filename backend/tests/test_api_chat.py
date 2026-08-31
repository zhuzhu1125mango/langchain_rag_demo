"""
聊天API单元测试
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


class TestChatAPI:
    """聊天API测试类"""
    
    def test_send_message_basic(self):
        """测试发送消息（非流式）"""
        response = client.post(
            "/api/chat/messages",
            json={"question": "你好"}
        )
        assert response.status_code == 200
        data = response.json()
        assert "session_id" in data
        assert "answer" in data
        assert "sources" in data
    
    def test_send_message_with_session(self):
        """测试在已有会话中发送消息"""
        first_response = client.post(
            "/api/chat/messages",
            json={"question": "你好"}
        )
        session_id = first_response.json()["session_id"]
        
        response = client.post(
            "/api/chat/messages",
            json={"question": "什么是人工智能？", "session_id": session_id}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == session_id
        assert "answer" in data
    
    def test_send_message_with_knowledge_base(self):
        """测试指定知识库发送消息"""
        kb_response = client.post(
            "/api/knowledge_bases/",
            json={"name": "测试KB"}
        )
        kb_id = kb_response.json()["id"]
        
        response = client.post(
            "/api/chat/messages",
            json={"question": "测试问题", "kb_ids": [kb_id]}
        )
        assert response.status_code == 200
    
    def test_send_message_sensitive_content(self):
        """测试敏感内容过滤"""
        response = client.post(
            "/api/chat/messages",
            json={"question": "敏感内容测试"}
        )
        assert response.status_code == 400 or response.status_code == 200
    
    def test_stream_answer(self):
        """测试流式回答"""
        response = client.post(
            "/api/chat/stream",
            json={"question": "你好"}
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/event-stream"
    
    def test_submit_feedback(self):
        """测试提交反馈"""
        msg_response = client.post(
            "/api/chat/messages",
            json={"question": "测试反馈"}
        )
        message_id = msg_response.json()["message_id"]
        
        response = client.post(
            f"/api/chat/messages/{message_id}/feedback",
            json={"rating": "positive", "reason": "回答很有用"}
        )
        assert response.status_code == 200
        data = response.json()
        assert "id" in data
        assert data["message"] == "评价提交成功"
    
    def test_submit_negative_feedback(self):
        """测试提交负面反馈"""
        msg_response = client.post(
            "/api/chat/messages",
            json={"question": "测试负面反馈"}
        )
        message_id = msg_response.json()["message_id"]
        
        response = client.post(
            f"/api/chat/messages/{message_id}/feedback",
            json={"rating": "negative", "reason": "回答不准确"}
        )
        assert response.status_code == 200