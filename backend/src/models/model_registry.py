"""模型 Provider / Deployment / Route 持久化模型（P2）。

API Key 只存 Fernet 密文；GET 接口永不返回明文。
"""

import uuid

from sqlalchemy import BigInteger, Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.sql import func

from src.database import Base


def _uuid_str() -> str:
    return str(uuid.uuid4())


class ModelProvider(Base):
    __tablename__ = "model_providers"

    id = Column(String(36), primary_key=True, default=_uuid_str)
    name = Column(String(64), unique=True, nullable=False)
    provider_type = Column(String(32), nullable=False, default="ollama")
    base_url = Column(String(512), nullable=True)
    api_key_ciphertext = Column(Text, nullable=True)
    api_key_env = Column(String(128), nullable=True)
    api_key_required = Column(Boolean, nullable=False, default=True)
    headers = Column(JSON, nullable=False, default=dict)
    timeout_s = Column(Float, nullable=False, default=60.0)
    external = Column(Boolean, nullable=False, default=True)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class ModelDeployment(Base):
    __tablename__ = "model_deployments"

    id = Column(String(36), primary_key=True, default=_uuid_str)
    provider_id = Column(String(36), ForeignKey("model_providers.id", ondelete="CASCADE"), nullable=False, index=True)
    model_name = Column(String(256), nullable=False)
    display_name = Column(String(128), nullable=False, default="")
    capabilities = Column(JSON, nullable=False, default=list)
    context_window = Column(Integer, nullable=False, default=0)
    max_output_tokens = Column(Integer, nullable=False, default=0)
    embedding_dimension = Column(Integer, nullable=False, default=0)
    temperature = Column(Float, nullable=True)
    # 成本估算单价（每 1K tokens）；未配置时该模型调用记为未定价，不估算成本
    input_cost_per_1k = Column(Float, nullable=True)
    output_cost_per_1k = Column(Float, nullable=True)
    price_currency = Column(String(8), nullable=False, default="USD", server_default="USD")
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class ModelRoute(Base):
    __tablename__ = "model_routes"

    id = Column(String(36), primary_key=True, default=_uuid_str)
    task = Column(String(64), unique=True, nullable=False)
    primary_model_id = Column(String(36), ForeignKey("model_deployments.id"), nullable=False)
    fallback_model_ids = Column(JSON, nullable=False, default=list)
    reasoning = Column(String(16), nullable=False, default="auto")
    temperature = Column(Float, nullable=True)
    max_output_tokens = Column(Integer, nullable=True)
    timeout_s = Column(Float, nullable=True)
    allow_external_fallback = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class ModelUsageDaily(Base):
    """按天聚合的模型用量（MODEL-P2.1）。

    不建外键：Provider/模型删除后历史用量仍需保留。主键含币种，避免同一模型
    切换计价币种后把不同币种金额相加。
    """

    __tablename__ = "model_usage_daily"

    # 按日期范围查询走独立索引（与迁移 b3c4d5e6f7a8 的 ix_model_usage_daily_usage_date 对齐）
    usage_date = Column(Date, primary_key=True, nullable=False, index=True)
    provider_id = Column(String(36), primary_key=True, nullable=False)
    model_id = Column(String(36), primary_key=True, nullable=False)
    task = Column(String(64), primary_key=True, nullable=False)
    cost_currency = Column(String(8), primary_key=True, nullable=False, default="USD")
    model_name = Column(String(256), nullable=False, default="")
    calls = Column(Integer, nullable=False, default=0)
    errors = Column(Integer, nullable=False, default=0)
    input_tokens = Column(BigInteger, nullable=False, default=0)
    output_tokens = Column(BigInteger, nullable=False, default=0)
    total_latency_ms = Column(Float, nullable=False, default=0.0)
    priced_calls = Column(Integer, nullable=False, default=0)
    unpriced_calls = Column(Integer, nullable=False, default=0)
    estimated_cost = Column(Float, nullable=False, default=0.0)
    last_error = Column(Text, nullable=False, default="")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ModelIndexProfile(Base):
    """Milvus 向量索引的 Embedding 画像（MODEL-P3）。

    一条记录对应一个 collection，记录当前索引由哪个 Embedding 模型/维度构建，
    以及最近一次重建前的画像与备份 collection，用于安全闸门与回滚。
    """

    __tablename__ = "model_index_profile"

    collection_name = Column(String(128), primary_key=True)
    profile_id = Column(String(512), nullable=False, default="")
    provider_id = Column(String(36), nullable=False, default="")
    model_id = Column(String(36), nullable=False, default="")
    model_name = Column(String(256), nullable=False, default="")
    dimension = Column(Integer, nullable=False, default=0)
    status = Column(String(16), nullable=False, default="ready")  # ready | rebuilding | failed | rolled_back
    chunk_count = Column(BigInteger, nullable=False, default=0)
    previous_profile_id = Column(String(512), nullable=True)
    previous_model_id = Column(String(36), nullable=True)
    previous_dimension = Column(Integer, nullable=True)
    backup_collection = Column(String(128), nullable=True)
    last_error = Column(Text, nullable=False, default="")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
