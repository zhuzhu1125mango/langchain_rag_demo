"""
数据库迁移脚本 - 创建实验相关表

为 A/B 测试功能创建所需的数据库表：
- experiments: 实验主表
- experiment_variants: 实验变体表
- traffic_allocations: 流量分配记录表
- experiment_metrics: 实验指标记录表
- experiment_results: 实验结果表

使用方法:
    cd backend && python scripts/migrate_experiment_tables.py
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import async_engine
from sqlalchemy import text


async def create_experiment_tables():
    """创建实验相关的数据库表"""
    print("=== 创建实验相关表 ===")

    async with async_engine.begin() as conn:
        # 安全删除旧表（如果存在）
        drop_statements = [
            "DROP TABLE IF EXISTS experiment_results CASCADE",
            "DROP TABLE IF EXISTS experiment_metrics CASCADE",
            "DROP TABLE IF EXISTS traffic_allocations CASCADE",
            "DROP TABLE IF EXISTS experiment_variants CASCADE",
            "DROP TABLE IF EXISTS experiments CASCADE",
        ]
        for stmt in drop_statements:
            await conn.execute(text(stmt))

        # 创建新表
        create_statements = [
            """
            CREATE TABLE experiments (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                name VARCHAR NOT NULL,
                description VARCHAR,
                status VARCHAR DEFAULT 'created',
                variants JSONB NOT NULL,
                metrics JSONB DEFAULT '["accuracy", "response_time"]',
                traffic_allocation FLOAT DEFAULT 1.0,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP WITH TIME ZONE,
                ended_at TIMESTAMP WITH TIME ZONE
            )
            """,
            """
            CREATE TABLE experiment_variants (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                experiment_id UUID NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
                name VARCHAR NOT NULL,
                weight FLOAT DEFAULT 0.5,
                config JSONB NOT NULL,
                enabled BOOLEAN DEFAULT TRUE
            )
            """,
            """
            CREATE TABLE traffic_allocations (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                experiment_id UUID NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
                variant_id VARCHAR,
                user_id VARCHAR,
                session_id VARCHAR,
                allocated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE experiment_metrics (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                experiment_id UUID NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
                variant_id VARCHAR,
                metric_name VARCHAR NOT NULL,
                metric_value FLOAT NOT NULL,
                recorded_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE experiment_results (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                experiment_id UUID NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
                winning_variant_id VARCHAR,
                analysis_data JSONB,
                confidence FLOAT,
                conclusion VARCHAR,
                analyzed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
            """,
        ]
        for stmt in create_statements:
            await conn.execute(text(stmt))

    print(" experiments          - 实验主表")
    print(" experiment_variants  - 实验变体表")
    print(" traffic_allocations  - 流量分配记录表")
    print(" experiment_metrics   - 实验指标记录表")
    print(" experiment_results   - 实验结果表")
    print("=== 迁移完成 ===")


if __name__ == "__main__":
    asyncio.run(create_experiment_tables())
