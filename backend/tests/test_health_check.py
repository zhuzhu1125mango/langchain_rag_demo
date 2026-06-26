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
    
    def test_health_check_detail(self):
        """测试详细健康检查接口"""
        response = client.get("/health/detail")
        
        assert response.status_code == 200
        data = response.json()
        assert "status" in data
        assert "checks" in data
    
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