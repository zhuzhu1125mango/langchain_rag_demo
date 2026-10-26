"""模型 Provider / Deployment / Route 持久化模型（P2）。

API Key 只存 Fernet 密文；GET 接口永不返回明文。
"""

import uuid

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, JSON, String, Text
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
