"""
健康检查单元测试
"""

import pytest
from fastapi.testclient import TestClient
from src.main import app

client = TestClient(app)


class TestHealthCheck:
    """健康检查测试类"""
    
    def test_health_check(self):
        """测试健康检查接口"""
        response = client.get("/health")
        
        assert response.status_code == 200
        data = response.json()
        assert "status" in data
        assert data["status"] == "healthy"
    
    def test_health_check_detail_requires_admin(self, monkeypatch):
        """详细健康检查接口需管理员权限（暴露依赖拓扑）。

        开发模式未配置 API_KEY 时匿名放行，因此通过 monkeypatch 强制启用
        认证（配置 API_KEY 且模拟 Docker），验证匿名请求被拒绝为 401/403。
        """
        from src.config import settings

        monkeypatch.setattr(settings, "IN_DOCKER", True)
        monkeypatch.setattr(settings.security, "API_KEY", "test-key")
        response = client.get("/health/detail")

        assert response.status_code in (401, 403)
    
    def test_root_endpoint(self):
        """测试根路径接口"""
        response = client.get("/")
        
        assert response.status_code == 200
        data = response.json()
        assert "message" in data
        assert "RAG Knowledge Base QA System API" in data["message"]
    
    def test_metrics_endpoint(self):
        """测试指标接口"""
        response = client.get("/metrics")
        
        assert response.status_code == 200
    
    def test_metrics_reset(self):
        """测试重置指标接口"""
        response = client.post("/metrics/reset")
        
        assert response.status_code == 200
        data = response.json()
        assert "message" in data
        assert data["message"] == "监控指标已重置"