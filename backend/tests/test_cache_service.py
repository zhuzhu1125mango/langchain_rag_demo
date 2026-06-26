"""
缓存服务单元测试
"""

import pytest
from src.services.cache_service import CacheService
from datetime import timedelta


class TestCacheService:
    """缓存服务测试类"""
    
    def setup_method(self):
        """每个测试前初始化缓存服务"""
        self.cache = CacheService()
    
    def test_cache_set_and_get(self):
        """测试缓存设置和获取"""
        self.cache.set("test_key", "test_value")
        result = self.cache.get("test_key")
        
        assert result == "test_value"
    
    def test_cache_set_and_get_dict(self):
        """测试缓存字典类型数据"""
        test_dict = {"name": "test", "value": 123}
        self.cache.set("test_dict", test_dict)
        result = self.cache.get("test_dict")
        
        assert isinstance(result, dict)
        assert result["name"] == "test"
        assert result["value"] == 123
    
    def test_cache_set_and_get_list(self):
        """测试缓存列表类型数据"""
        test_list = [1, 2, 3, 4, 5]
        self.cache.set("test_list", test_list)
        result = self.cache.get("test_list")
        
        assert isinstance(result, list)
        assert result == [1, 2, 3, 4, 5]
    
    def test_cache_get_nonexistent(self):
        """测试获取不存在的缓存"""
        result = self.cache.get("nonexistent_key")
        
        assert result is None
    
    def test_cache_delete(self):
        """测试删除缓存"""
        self.cache.set("test_delete", "value")
        assert self.cache.exists("test_delete") is True
        
        self.cache.delete("test_delete")
        assert self.cache.exists("test_delete") is False
    
    def test_cache_exists(self):
        """测试缓存存在检查"""
        self.cache.set("test_exists", "value")
        assert self.cache.exists("test_exists") is True
        assert self.cache.exists("nonexistent") is False
    
    def test_cache_with_expiry(self):
        """测试带过期时间的缓存"""
        self.cache.set("test_expiry", "value", expire=timedelta(seconds=1))
        
        result = self.cache.get("test_expiry")
        assert result == "value"
    
    def test_cache_clear_pattern(self):
        """测试按模式清除缓存"""
        self.cache.set("prefix_key1", "value1")
        self.cache.set("prefix_key2", "value2")
        self.cache.set("other_key", "value3")
        
        self.cache.clear_pattern("prefix*")
        
        assert self.cache.get("prefix_key1") is None
        assert self.cache.get("prefix_key2") is None
        assert self.cache.get("other_key") == "value3"