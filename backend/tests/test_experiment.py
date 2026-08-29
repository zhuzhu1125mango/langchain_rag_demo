"""
A/B测试框架单元测试
"""

import pytest

# 依赖真实 PostgreSQL（实验记录读写），默认跳过；与 asyncio 标记合并
import asyncio
from src.services.experiment_manager import ExperimentManager

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]


class TestExperimentManager:
    """
    实验管理器测试
    """

    def setup_method(self):
        self.manager = ExperimentManager()

    async def test_create_experiment(self):
        """测试创建实验"""
        variants = [
            {"name": "Control", "weight": 0.5, "config": {"strategy": "original"}},
            {"name": "Variant A", "weight": 0.5, "config": {"strategy": "new"}}
        ]

        experiment_id = await self.manager.create_experiment(
            name="Test Experiment",
            description="Test description",
            variants=variants,
            metrics=["accuracy", "response_time"]
        )

        assert experiment_id is not None
        assert isinstance(experiment_id, str)

        # 验证初始状态为 created
        experiment = await self.manager.get_experiment(experiment_id)
        assert experiment is not None
        assert experiment["status"] == "created"

    async def test_traffic_allocation_consistency(self):
        """测试流量分配一致性"""
        user_id = "test_user_123"
        experiment_id = "exp_test_001"

        hash_value1 = self.manager._hash_user_id(user_id, experiment_id)
        hash_value2 = self.manager._hash_user_id(user_id, experiment_id)

        assert hash_value1 == hash_value2

    async def test_hash_distribution(self):
        """测试哈希分布"""
        experiment_id = "exp_test_hash"
        hash_values = []

        for i in range(100):
            user_id = f"user_{i}"
            hash_value = self.manager._hash_user_id(user_id, experiment_id)
            hash_values.append(hash_value)

        assert all(0.0 <= h <= 1.0 for h in hash_values)

        avg_hash = sum(hash_values) / len(hash_values)
        assert 0.4 <= avg_hash <= 0.6

    async def test_analyze_experiment_no_data(self):
        """测试分析无数据实验"""
        result = await self.manager.analyze_experiment("exp_no_data")

        assert result["status"] == "no_data"

    async def test_analyze_experiment_with_data(self):
        """测试分析有数据实验，验证返回数据结构"""
        variants = [
            {"id": "v1", "name": "Control", "weight": 0.5, "config": {"strategy": "original"}},
            {"id": "v2", "name": "Variant A", "weight": 0.5, "config": {"strategy": "new"}}
        ]

        experiment_id = await self.manager.create_experiment(
            name="Analysis Test",
            variants=variants,
            metrics=["accuracy"]
        )

        # 记录指标数据
        await self.manager.record_metric(experiment_id, "v1", "accuracy", 0.8)
        await self.manager.record_metric(experiment_id, "v1", "accuracy", 0.9)
        await self.manager.record_metric(experiment_id, "v2", "accuracy", 0.75)
        await self.manager.record_metric(experiment_id, "v2", "accuracy", 0.78)

        # 分析实验
        result = await self.manager.analyze_experiment(experiment_id)

        assert result["status"] == "completed"
        assert "variant_results" in result
        assert isinstance(result["variant_results"], list)
        assert len(result["variant_results"]) == 2
        assert result["winner_variant_id"] is not None
        assert result["confidence_level"] > 0

        # 验证变体结果结构
        for vr in result["variant_results"]:
            assert "variant_id" in vr
            assert "variant_name" in vr
            assert "sample_size" in vr
            assert "metrics" in vr
            assert "accuracy" in vr["metrics"]
            assert "mean" in vr["metrics"]["accuracy"]
            assert "std" in vr["metrics"]["accuracy"]
            assert "count" in vr["metrics"]["accuracy"]

    async def test_batch_delete_experiments(self):
        """测试批量删除实验及其关联数据"""
        # 创建两个实验并记录指标
        experiment_id_1 = await self.manager.create_experiment(
            name="Delete Test 1",
            variants=[{"id": "v1", "name": "Control", "weight": 1.0, "config": {}}],
            metrics=["accuracy"]
        )
        experiment_id_2 = await self.manager.create_experiment(
            name="Delete Test 2",
            variants=[{"id": "v1", "name": "Control", "weight": 1.0, "config": {}}],
            metrics=["accuracy"]
        )

        await self.manager.record_metric(experiment_id_1, "v1", "accuracy", 0.8)
        await self.manager.record_metric(experiment_id_2, "v1", "accuracy", 0.9)

        # 批量删除
        result = await self.manager.delete_experiments([experiment_id_1, experiment_id_2])

        assert result["success_count"] == 2
        assert result["failed_count"] == 0
        assert len(result["failed"]) == 0

        # 验证实验已删除
        assert await self.manager.get_experiment(experiment_id_1) is None
        assert await self.manager.get_experiment(experiment_id_2) is None

        # 验证关联指标已级联清理
        metrics = await self.manager.get_metrics(experiment_id_1)
        assert not metrics
        metrics = await self.manager.get_metrics(experiment_id_2)
        assert not metrics

    async def test_batch_delete_experiments_with_invalid_ids(self):
        """测试批量删除包含无效ID和不存在的ID"""
        experiment_id = await self.manager.create_experiment(
            name="Valid Experiment",
            variants=[{"id": "v1", "name": "Control", "weight": 1.0, "config": {}}],
            metrics=["accuracy"]
        )

        result = await self.manager.delete_experiments(
            [experiment_id, "invalid-uuid", "550e8400-e29b-41d4-a716-446655440999"]
        )

        assert result["success_count"] == 1
        assert result["failed_count"] == 2
        assert len(result["failed"]) == 2

        # 验证有效实验已删除
        assert await self.manager.get_experiment(experiment_id) is None

        # 验证失败原因
        invalid_failure = next(
            (f for f in result["failed"] if f["experiment_id"] == "invalid-uuid"), None
        )
        assert invalid_failure is not None
        assert "无效" in invalid_failure["reason"]

        not_found_failure = next(
            (f for f in result["failed"] if "550e8400" in f["experiment_id"]), None
        )
        assert not_found_failure is not None
        assert "不存在" in not_found_failure["reason"]