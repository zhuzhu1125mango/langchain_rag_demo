"""Milvus 集合结构：schema 构建与增量字段/索引升级（CODE-1 拆分）。"""

import asyncio
import logging
from typing import Optional

from pymilvus import AsyncMilvusClient, DataType

from src.config import settings

logger = logging.getLogger("milvus_service")


class SchemaMixin:
    """集合生命周期中的 schema/索引部分，由 MilvusService 组合。"""

    async def _ensure_collection(self):
        """确保集合存在，schema 包含 dense + sparse 向量字段及对应索引。"""
        has_collection = await self.client.has_collection(settings.milvus.MILVUS_COLLECTION_NAME)
        if not has_collection:
            # 集合新建意味着历史 sparse 向量已清空，重置持久化词表（与 collection 版本绑定）
            await asyncio.to_thread(self._remove_bm25_vocabulary)
            schema = await self._build_schema()
            await self.client.create_collection(
                collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
                schema=schema
            )
            await self._create_indexes()
        else:
            await self._add_heading_path_field_if_missing()
            await self._add_source_kind_field_if_missing()
            await self._add_kb_id_field_if_missing()
            await self._ensure_dimension_match()
            await self._add_sparse_field_if_missing()
            await self._ensure_index()
            await self._ensure_sparse_index()
        await self.client.load_collection(settings.milvus.MILVUS_COLLECTION_NAME)

    async def _build_schema(self, dimension: Optional[int] = None):
        """构建包含 dense 与 sparse 向量的集合 schema。"""
        schema = AsyncMilvusClient.create_schema(
            auto_id=False,
            enable_dynamic_field=False
        )
        schema.add_field("id", DataType.VARCHAR, max_length=64, is_primary=True)
        schema.add_field("kb_id", DataType.VARCHAR, max_length=64, default_value="")
        schema.add_field("document_id", DataType.VARCHAR, max_length=64)
        schema.add_field("content", DataType.VARCHAR, max_length=65535, enable_analyzer=True)
        schema.add_field(
            "embedding",
            DataType.FLOAT_VECTOR,
            dim=int(dimension or self._embedding_dimension()),
        )
        schema.add_field("source", DataType.VARCHAR, max_length=512)
        schema.add_field("chunk_index", DataType.INT64)
        # P0-2a：结构化分块的标题路径（"A > B > C"），旧集合通过 add_collection_field 兼容
        schema.add_field("heading_path", DataType.VARCHAR, max_length=512, default_value="")
        self._has_heading_path = True
        # P2 LLM-Wiki：chunk 来源类型（raw=原始文档 / wiki=编译页），旧集合在线补加
        schema.add_field("source_kind", DataType.VARCHAR, max_length=16, default_value="raw")
        self._has_source_kind = True
        # 阶段一：BM25 sparse vector，维度由 BM25 词表动态决定
        if self._sparse_enabled:
            schema.add_field("sparse_embedding", DataType.SPARSE_FLOAT_VECTOR)
        return schema

    async def _add_heading_path_field_if_missing(self):
        """兼容旧集合：heading_path 字段缺失时在线补加（Milvus 2.6 add_collection_field）。

        补加失败（如服务端版本过旧）仅告警，插入/查询自动降级不携带该字段，
        不阻断启动；结构化分块的其他收益（内容前置标题路径）不受影响。
        """
        collection_info = await self.client.describe_collection(settings.milvus.MILVUS_COLLECTION_NAME)
        field_names = [f["name"] for f in collection_info["fields"]]
        if "heading_path" in field_names:
            self._has_heading_path = True
            return
        try:
            await self.client.add_collection_field(
                collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
                field_name="heading_path",
                data_type=DataType.VARCHAR,
                desc="结构化分块的标题路径（A > B > C）",
                max_length=512,
                nullable=True,
                default_value="",
            )
            self._has_heading_path = True
            logger.info("已为现有集合在线补加 heading_path 字段")
        except Exception as e:
            self._has_heading_path = False
            logger.warning(f"在线补加 heading_path 字段失败，插入/查询将不携带该字段: {e}")

    async def _add_source_kind_field_if_missing(self):
        """兼容旧集合：source_kind 字段缺失时在线补加（同 heading_path 路径）。

        补加失败仅告警，插入自动降级不携带该字段（默认按 raw 处理），不阻断启动。
        """
        collection_info = await self.client.describe_collection(settings.milvus.MILVUS_COLLECTION_NAME)
        field_names = [f["name"] for f in collection_info["fields"]]
        if "source_kind" in field_names:
            self._has_source_kind = True
            return
        try:
            await self.client.add_collection_field(
                collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
                field_name="source_kind",
                data_type=DataType.VARCHAR,
                desc="chunk 来源类型（raw=原始文档 / wiki=LLM 编译页）",
                max_length=16,
                nullable=True,
                default_value="raw",
            )
            self._has_source_kind = True
            logger.info("已为现有集合在线补加 source_kind 字段")
        except Exception as e:
            self._has_source_kind = False
            logger.warning(f"在线补加 source_kind 字段失败，插入将不携带该字段: {e}")

    def _base_output_fields(self) -> list:
        """检索/查询的基础 output_fields，heading_path 视 schema 能力动态附加。"""
        fields = ["kb_id", "document_id", "content", "source", "chunk_index"]
        if self._has_heading_path:
            fields.append("heading_path")
        if self._has_source_kind:
            fields.append("source_kind")
        return fields

    async def _create_indexes(self, collection_name: Optional[str] = None):
        """创建 dense HNSW 索引与 sparse 索引。"""
        target = collection_name or settings.milvus.MILVUS_COLLECTION_NAME
        # Dense HNSW 索引
        index_params = self.client.prepare_index_params(
            field_name="embedding",
            metric_type="IP",
            index_type="HNSW",
            params={
                "M": settings.milvus.MILVUS_HNSW_M,
                "efConstruction": settings.milvus.MILVUS_EF_CONSTRUCTION,
            },
            index_name=settings.milvus.MILVUS_INDEX_NAME
        )
        await self.client.create_index(
            collection_name=target,
            index_params=index_params
        )
        # Sparse BM25 索引
        if self._sparse_enabled:
            sparse_index_params = self.client.prepare_index_params(
                field_name="sparse_embedding",
                metric_type="IP",
                index_type="SPARSE_INVERTED_INDEX",
                params={"drop_ratio_build": 0.2},
                index_name=f"{settings.milvus.MILVUS_INDEX_NAME}_sparse"
            )
            await self.client.create_index(
                collection_name=target,
                index_params=sparse_index_params
            )

    async def _add_kb_id_field_if_missing(self):
        """兼容旧集合：缺少 kb_id 字段时按 MILVUS_REBUILD_ON_MISMATCH 处理。

        默认 False：直接启动失败并提示，避免静默清空全部向量数据；
        显式开启后删除重建（旧数据丢失，需重新导入或提前迁移）。
        """
        collection_info = await self.client.describe_collection(settings.milvus.MILVUS_COLLECTION_NAME)
        field_names = [f["name"] for f in collection_info["fields"]]
        if "kb_id" not in field_names:
            if not settings.milvus.MILVUS_REBUILD_ON_MISMATCH:
                raise RuntimeError(
                    "Milvus 集合缺少 kb_id 字段（schema 升级）。删除重建会丢失全部向量数据，"
                    "默认已禁止。请先完成数据迁移/备份，再设置 MILVUS_REBUILD_ON_MISMATCH=true 重启，"
                    "或运行迁移脚本重建集合并回填数据。"
                )
            logger.warning(
                "集合缺少 kb_id 字段且 MILVUS_REBUILD_ON_MISMATCH=true，执行删除重建，旧数据将丢失"
            )
            await self.client.drop_collection(settings.milvus.MILVUS_COLLECTION_NAME)
            schema = await self._build_schema()
            await self.client.create_collection(
                collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
                schema=schema
            )
            await self._create_indexes()

    async def _ensure_dimension_match(self):
        """兼容旧集合：embedding 维度与当前配置不一致时按 MILVUS_REBUILD_ON_MISMATCH 处理。

        切换 Embedding 模型（如 nomic-embed-text 768 维 → bge-m3 1024 维）
        时，Milvus 不支持直接修改向量字段维度。默认 False：不一致时启动失败，
        避免静默清空数据；显式开启后才删除重建（旧数据丢失，需重新导入）。
        """
        collection_info = await self.client.describe_collection(settings.milvus.MILVUS_COLLECTION_NAME)
        for field in collection_info["fields"]:
            if field["name"] == "embedding" and field["type"] == DataType.FLOAT_VECTOR:
                current_dim = field.get("params", {}).get("dim")
                expected_dim = self._embedding_dimension()
                if current_dim is not None and current_dim != expected_dim:
                    if not settings.milvus.MILVUS_REBUILD_ON_MISMATCH:
                        raise RuntimeError(
                            f"Milvus 集合 embedding 维度不一致: 当前 {current_dim}, 期望 {expected_dim}"
                            "（通常是切换了 Embedding 模型）。删除重建会丢失全部向量数据，默认已禁止。"
                            "请先完成数据迁移/备份，再设置 MILVUS_REBUILD_ON_MISMATCH=true 重启。"
                        )
                    logger.warning(
                        f"集合 embedding 维度不一致: 当前 {current_dim}, 期望 {expected_dim}，"
                        "MILVUS_REBUILD_ON_MISMATCH=true，执行删除重建，旧数据将丢失"
                    )
                    await self.client.drop_collection(settings.milvus.MILVUS_COLLECTION_NAME)
                    schema = await self._build_schema()
                    await self.client.create_collection(
                        collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
                        schema=schema
                    )
                    await self._create_indexes()
                break

    async def describe_embedding_dimension(self, collection_name: Optional[str] = None) -> Optional[int]:
        """返回集合 embedding 字段维度；集合不存在时返回 None。"""
        name = collection_name or settings.milvus.MILVUS_COLLECTION_NAME
        if not await self.client.has_collection(name):
            return None
        info = await self.client.describe_collection(name)
        for field in info.get("fields", []):
            if field.get("name") == "embedding" and field.get("type") == DataType.FLOAT_VECTOR:
                return int(field.get("params", {}).get("dim") or 0) or None
        return None

    async def _collection_count(self, collection_name: str) -> int:
        if not await self.client.has_collection(collection_name):
            return 0
        rows = await self.client.query(
            collection_name=collection_name,
            filter="",
            output_fields=["count(*)"],
        )
        return int(rows[0]["count(*)"] if rows else 0)

    async def _add_sparse_field_if_missing(self):
        """兼容旧集合：缺少 sparse_embedding 字段时不删除数据，仅记录日志。

        由于 Milvus 不支持向已有集合新增向量字段，完整迁移需使用
        backend/scripts/migrate_hybrid_index.py 脚本重建集合并重新插入数据。
        """
        try:
            collection_info = await self.client.describe_collection(settings.milvus.MILVUS_COLLECTION_NAME)
            field_names = [f["name"] for f in collection_info["fields"]]
            if self._sparse_enabled and "sparse_embedding" not in field_names:
                logger.warning(
                    "当前集合缺少 sparse_embedding 字段，混合检索已自动降级为纯向量检索。"
                    "如需启用混合检索，请运行迁移脚本: python backend/scripts/migrate_hybrid_index.py"
                )
                self._sparse_enabled = False
        except Exception as e:
            # schema 兼容检查失败不能静默：无法确认字段存在性会使降级判断失效。
            # 此处不阻塞启动（保持 _sparse_enabled 不变），后续插入/检索路径
            # 对 sparse 字段缺失已有独立降级兜底。
            logger.error(f"检查集合 sparse_embedding 字段时失败: {e}", exc_info=True)

    async def _ensure_index(self):
        """确保 dense HNSW 索引存在；若不存在则创建。"""
        try:
            indexes = await self.client.list_indexes(settings.milvus.MILVUS_COLLECTION_NAME)
            if settings.milvus.MILVUS_INDEX_NAME not in indexes:
                index_params = self.client.prepare_index_params(
                    field_name="embedding",
                    metric_type="IP",
                    index_type="HNSW",
                    params={
                        "M": settings.milvus.MILVUS_HNSW_M,
                        "efConstruction": settings.milvus.MILVUS_EF_CONSTRUCTION,
                    },
                    index_name=settings.milvus.MILVUS_INDEX_NAME
                )
                await self.client.create_index(
                    collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
                    index_params=index_params
                )
        except Exception as e:
            # 索引创建失败会显著劣化检索性能，不能静默吞掉
            logger.error(f"确保 dense HNSW 索引存在时失败: {e}", exc_info=True)

    async def _ensure_sparse_index(self):
        """确保 sparse BM25 索引存在；若不存在则创建。"""
        if not self._sparse_enabled:
            return
        try:
            indexes = await self.client.list_indexes(settings.milvus.MILVUS_COLLECTION_NAME)
            sparse_index_name = f"{settings.milvus.MILVUS_INDEX_NAME}_sparse"
            if sparse_index_name not in indexes:
                sparse_index_params = self.client.prepare_index_params(
                    field_name="sparse_embedding",
                    metric_type="IP",
                    index_type="SPARSE_INVERTED_INDEX",
                    params={"drop_ratio_build": 0.2},
                    index_name=sparse_index_name
                )
                await self.client.create_index(
                    collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
                    index_params=sparse_index_params
                )
        except Exception as e:
            logger.error(f"确保 sparse BM25 索引存在时失败: {e}", exc_info=True)
