"""Milvus 向量数据库服务。

负责与 Milvus 交互，提供集合管理、向量插入、相似度检索、按文档/知识库删除等功能。
使用单例模式避免重复初始化连接与 Embedding 模型。

阶段一升级：支持 dense + sparse（BM25）双通道混合检索。

CODE-1 拆分：实现按关注点拆到同目录 mixin，本模块只保留连接生命周期与
Embedding 实例管理，类组合后对外接口完全不变。

- `milvus_common`     BM25 可选后端与 A/B rerank 参数（模块级共享依赖）
- `milvus_schema`     集合 schema / 字段与索引升级
- `milvus_migration`  影子集合重建、原子切换与回滚
- `milvus_writer`     向量插入、BM25 词表、限速 flush
- `milvus_search`     dense / sparse / 混合检索与结果规整
- `milvus_admin`      删除、计数与连接维护
"""

import asyncio
import logging
from typing import Any, Optional

from aiolimiter import AsyncLimiter
from pymilvus import AsyncMilvusClient

from src.config import settings
from src.services.milvus_admin import AdminMixin
from src.services.milvus_common import (  # noqa: F401  兼容历史导入（测试直接引用）
    _BM25_AVAILABLE,
    _ab_rerank_top_k,
)
from src.services.milvus_migration import MigrationMixin
from src.services.milvus_schema import SchemaMixin
from src.services.milvus_search import SearchMixin
from src.services.milvus_writer import WriteMixin
from src.services.model_manager import model_manager
from src.utils.async_singleton import AsyncSingleton

logger = logging.getLogger("milvus_service")


class MilvusService(
    AsyncSingleton["MilvusService"],
    SchemaMixin,
    MigrationMixin,
    WriteMixin,
    SearchMixin,
    AdminMixin,
):
    """Milvus 客户端封装，管理集合生命周期与向量操作。"""

    def __init__(self):
        self.client: AsyncMilvusClient | None = None
        self.embeddings: Any | None = None
        # 阶段一：BM25 稀疏嵌入模型与开关
        self.bm25_ef: Optional[Any] = None
        self._sparse_enabled: bool = settings.processing.KB_ENABLE_HYBRID_SEARCH and _BM25_AVAILABLE
        # Flush 令牌桶：按配置限速，避免触发 Milvus 单集合 flush 限流。
        # AsyncLimiter(max_rate, time_period) 的 max_rate 同时是桶的最大容量，
        # 因此将 QPS 转换为 max_rate=1, time_period=1/QPS，确保 async with 获取 1 容量合法。
        flush_interval = max(1.0, 1.0 / settings.milvus.MILVUS_FLUSH_RATE_LIMIT)
        self._flush_limiter = AsyncLimiter(1, flush_interval)
        # schema 是否含 heading_path 字段（P0-2a 结构化分块），决定插入/查询是否携带
        self._has_heading_path: bool = False
        # P3：记录 embeddings 实例对应的 registry 模型标识，切换 Embedding 后自动刷新
        self._embeddings_key: str = ""

    def _embedding_dimension(self) -> int:
        """当前 Embedding 维度：registry 为权威，legacy/env 作为兜底。"""
        try:
            model = model_manager.registry.get_embedding_model().model
            return int(model.embedding_dimension or settings.model.EMBEDDING_DIMENSION)
        except Exception:
            return int(settings.model.EMBEDDING_DIMENSION)

    def _get_embeddings(self):
        """按当前 registry 获取共享 Embedding 实例；模型切换时自动刷新。"""
        try:
            key = model_manager.embedding_model_key
        except Exception:
            key = ""
        if self.embeddings is None or (key and key != self._embeddings_key):
            self.embeddings = model_manager.get_embeddings()
            self._embeddings_key = key
        return self.embeddings

    def refresh_embeddings(self) -> None:
        """索引迁移/registry 切换后强制刷新 Embedding 实例。"""
        self._embeddings_key = ""
        self.embeddings = None
        self._get_embeddings()

    async def _async_init(self):
        """异步初始化 Milvus 连接、Embedding 模型并确保集合就绪。"""
        self.client = AsyncMilvusClient(
            uri=f"http://{settings.milvus.MILVUS_HOST}:{settings.milvus.MILVUS_PORT}",
            db_name=settings.milvus.MILVUS_DATABASE
        )
        # B2：embeddings 走 model_manager 共享单例，避免各服务重复构造
        self._get_embeddings()
        await self._ensure_collection()
        # 启动时加载与 collection 绑定的持久化 BM25 词表，
        # 保证查询编码空间与历史 sparse 向量一致（不依赖"先插入过文档"）
        await asyncio.to_thread(self._load_bm25_vocabulary)

    async def _async_cleanup(self):
        """关闭 Milvus 客户端连接。"""
        if self.client is not None:
            try:
                await self.client.close()
            except Exception as e:
                logger.warning(f"关闭 Milvus 客户端失败: {e}")
            finally:
                self.client = None
                self.embeddings = None


__all__ = ["MilvusService"]
