"""
Milvus 破坏性重建防护测试（修复6）。

回归：此前集合 schema/维度不匹配时无条件删除重建且异常被静默吞掉，
任何 describe_collection 意外异常都会被 pass 掉，存在静默清空数据风险。
现在默认拒绝重建并启动失败，仅在 MILVUS_REBUILD_ON_MISMATCH=true 时放行。
"""

from unittest.mock import AsyncMock

import pytest
from pymilvus import DataType

from src.config import settings
from src.services.milvus_service import MilvusService


def _make_service() -> MilvusService:
    """构造不触发真实连接的 MilvusService，client 全部 mock。"""
    svc = MilvusService()
    svc.client = AsyncMock()
    # 重建路径中的 schema/索引构建替换为桩，隔离 pymilvus 细节
    svc._build_schema = AsyncMock(return_value="schema-sentinel")
    svc._create_indexes = AsyncMock()
    return svc


def _collection_info(fields):
    return {"fields": fields}


KB_FIELD = {"name": "kb_id", "type": DataType.VARCHAR}
EMBEDDING_FIELD_1024 = {
    "name": "embedding", "type": DataType.FLOAT_VECTOR, "params": {"dim": 1024}
}
EMBEDDING_FIELD_768 = {
    "name": "embedding", "type": DataType.FLOAT_VECTOR, "params": {"dim": 768}
}


@pytest.fixture
def rebuild_flag_off():
    """确保测试期间重建开关为 False，结束后恢复。"""
    original = settings.milvus.MILVUS_REBUILD_ON_MISMATCH
    settings.milvus.MILVUS_REBUILD_ON_MISMATCH = False
    yield
    settings.milvus.MILVUS_REBUILD_ON_MISMATCH = original


class TestKbIdFieldGuard:
    """kb_id 字段缺失场景。"""

    async def test_missing_kb_id_default_rejected(self, rebuild_flag_off):
        """默认配置：缺 kb_id 字段必须抛 RuntimeError，不得删除集合。"""
        svc = _make_service()
        svc.client.describe_collection.return_value = _collection_info([EMBEDDING_FIELD_1024])

        with pytest.raises(RuntimeError, match="kb_id"):
            await svc._add_kb_id_field_if_missing()

        svc.client.drop_collection.assert_not_called()
        svc.client.create_collection.assert_not_called()

    async def test_missing_kb_id_with_flag_rebuilds(self, rebuild_flag_off, monkeypatch):
        """显式开关开启：允许删除重建。"""
        monkeypatch.setattr(settings.milvus, "MILVUS_REBUILD_ON_MISMATCH", True)
        svc = _make_service()
        svc.client.describe_collection.return_value = _collection_info([EMBEDDING_FIELD_1024])

        await svc._add_kb_id_field_if_missing()

        svc.client.drop_collection.assert_awaited_once()
        svc.client.create_collection.assert_awaited_once()
        svc._create_indexes.assert_awaited_once()

    async def test_kb_id_present_no_rebuild(self, rebuild_flag_off):
        """字段齐全：任何配置下都不应触发重建。"""
        svc = _make_service()
        svc.client.describe_collection.return_value = _collection_info([KB_FIELD, EMBEDDING_FIELD_1024])

        await svc._add_kb_id_field_if_missing()

        svc.client.drop_collection.assert_not_called()
        svc.client.create_collection.assert_not_called()


class TestDimensionGuard:
    """embedding 维度不匹配场景。"""

    async def test_dim_mismatch_default_rejected(self, rebuild_flag_off):
        """默认配置：维度不一致必须抛 RuntimeError，不得删除集合。"""
        svc = _make_service()
        svc.client.describe_collection.return_value = _collection_info(
            [KB_FIELD, EMBEDDING_FIELD_768]
        )

        with pytest.raises(RuntimeError, match="维度不一致"):
            await svc._ensure_dimension_match()

        svc.client.drop_collection.assert_not_called()
        svc.client.create_collection.assert_not_called()

    async def test_dim_mismatch_with_flag_rebuilds(self, rebuild_flag_off, monkeypatch):
        """显式开关开启：维度不一致时允许删除重建。"""
        monkeypatch.setattr(settings.milvus, "MILVUS_REBUILD_ON_MISMATCH", True)
        svc = _make_service()
        svc.client.describe_collection.return_value = _collection_info(
            [KB_FIELD, EMBEDDING_FIELD_768]
        )

        await svc._ensure_dimension_match()

        svc.client.drop_collection.assert_awaited_once()
        svc.client.create_collection.assert_awaited_once()
        svc._create_indexes.assert_awaited_once()

    async def test_dim_match_no_rebuild(self, rebuild_flag_off):
        """维度一致：不触发重建。"""
        svc = _make_service()
        svc.client.describe_collection.return_value = _collection_info(
            [KB_FIELD, EMBEDDING_FIELD_1024]
        )

        await svc._ensure_dimension_match()

        svc.client.drop_collection.assert_not_called()


class TestNoSilentFailure:
    """回归：describe_collection 失败不得被静默吞掉。"""

    async def test_describe_error_propagates(self, rebuild_flag_off):
        svc = _make_service()
        svc.client.describe_collection.side_effect = ConnectionError("milvus down")

        with pytest.raises(ConnectionError):
            await svc._add_kb_id_field_if_missing()

        with pytest.raises(ConnectionError):
            await svc._ensure_dimension_match()

        svc.client.drop_collection.assert_not_called()
