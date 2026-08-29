"""Milvus 向量数据库服务。

负责与 Milvus 交互，提供集合管理、向量插入、相似度检索、按文档/知识库删除等功能。
使用单例模式避免重复初始化连接与 Embedding 模型。

阶段一升级：支持 dense + sparse（BM25）双通道混合检索。
"""

import asyncio
import os
import uuid
import logging
import re
import time
from typing import Any, Dict, List, Optional

from pymilvus import AsyncMilvusClient, DataType
from pymilvus.exceptions import MilvusException
from langchain_ollama import OllamaEmbeddings
from aiolimiter import AsyncLimiter
from src.config import settings, DATA_DIR
from src.utils.async_singleton import AsyncSingleton

logger = logging.getLogger("milvus_service")

# 可选的 BM25 稀疏嵌入模型，失败时优雅降级为纯向量检索
try:
    from pymilvus.model.sparse import BM25EmbeddingFunction
    from pymilvus.model.sparse.bm25.tokenizers import build_default_analyzer
    _BM25_AVAILABLE = True
except Exception as _bm25_import_err:  # pragma: no cover - 依赖未安装时降级
    BM25EmbeddingFunction = None
    build_default_analyzer = None
    _BM25_AVAILABLE = False
    logger.warning(f"pymilvus-model 未安装，知识库混合检索将降级为纯向量检索: {_bm25_import_err}")


class MilvusService(AsyncSingleton["MilvusService"]):
    """Milvus 客户端封装，管理集合生命周期与向量操作。"""

    def __init__(self):
        self.client: AsyncMilvusClient | None = None
        self.embeddings: OllamaEmbeddings | None = None
        # 阶段一：BM25 稀疏嵌入模型与开关
        self.bm25_ef: Optional[Any] = None
        self._sparse_enabled: bool = settings.processing.KB_ENABLE_HYBRID_SEARCH and _BM25_AVAILABLE
        # Flush 令牌桶：按配置限速，避免触发 Milvus 单集合 flush 限流。
        # AsyncLimiter(max_rate, time_period) 的 max_rate 同时是桶的最大容量，
        # 因此将 QPS 转换为 max_rate=1, time_period=1/QPS，确保 async with 获取 1 容量合法。
        flush_interval = max(1.0, 1.0 / settings.milvus.MILVUS_FLUSH_RATE_LIMIT)
        self._flush_limiter = AsyncLimiter(1, flush_interval)

    async def _async_init(self):
        """异步初始化 Milvus 连接、Embedding 模型并确保集合就绪。"""
        self.client = AsyncMilvusClient(
            uri=f"http://{settings.milvus.MILVUS_HOST}:{settings.milvus.MILVUS_PORT}",
            db_name=settings.milvus.MILVUS_DATABASE
        )
        self.embeddings = OllamaEmbeddings(model=settings.model.EMBEDDING_MODEL_NAME)
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
            await self._add_kb_id_field_if_missing()
            await self._ensure_dimension_match()
            await self._add_sparse_field_if_missing()
            await self._ensure_index()
            await self._ensure_sparse_index()
        await self.client.load_collection(settings.milvus.MILVUS_COLLECTION_NAME)

    async def _build_schema(self):
        """构建包含 dense 与 sparse 向量的集合 schema。"""
        schema = AsyncMilvusClient.create_schema(
            auto_id=False,
            enable_dynamic_field=False
        )
        schema.add_field("id", DataType.VARCHAR, max_length=64, is_primary=True)
        schema.add_field("kb_id", DataType.VARCHAR, max_length=64, default_value="")
        schema.add_field("document_id", DataType.VARCHAR, max_length=64)
        schema.add_field("content", DataType.VARCHAR, max_length=65535, enable_analyzer=True)
        schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=settings.model.EMBEDDING_DIMENSION)
        schema.add_field("source", DataType.VARCHAR, max_length=512)
        schema.add_field("chunk_index", DataType.INT64)
        # 阶段一：BM25 sparse vector，维度由 BM25 词表动态决定
        if self._sparse_enabled:
            schema.add_field("sparse_embedding", DataType.SPARSE_FLOAT_VECTOR)
        return schema

    async def _create_indexes(self):
        """创建 dense HNSW 索引与 sparse 索引。"""
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
            collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
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
                collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
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
                expected_dim = settings.model.EMBEDDING_DIMENSION
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

    async def insert_embeddings(self, documents, kb_id=""):
        """将文档切块后的 embedding 批量插入 Milvus。

        阶段一升级：同时插入 dense embedding 与 BM25 sparse embedding（如启用）。

        Args:
            documents: 待插入的 Document 列表，每个文档需包含 document_id/source/chunk_index 元数据。
            kb_id: 知识库 ID，用于按知识库过滤。

        Returns:
            int: 实际插入的切片数量。
        """
        contents = [doc.page_content for doc in documents]
        embeddings_list = await self.embeddings.aembed_documents(contents)

        # 阶段一：计算 BM25 sparse embedding
        sparse_embeddings = None
        if self._sparse_enabled and documents:
            try:
                await self._ensure_bm25_fitted(contents)
                sparse_embeddings = await asyncio.to_thread(self.bm25_ef.encode_documents, contents)
                sparse_embeddings = self._convert_sparse_embeddings(sparse_embeddings)
            except Exception as e:
                logger.warning(f"BM25 sparse embedding 计算失败，跳过 sparse 字段插入: {e}")
                sparse_embeddings = None

        data = []
        for i, (doc, embedding) in enumerate(zip(documents, embeddings_list)):
            chunk_id = str(uuid.uuid4())
            item = {
                "id": chunk_id,
                "kb_id": kb_id,
                "document_id": doc.metadata.get("document_id", ""),
                "content": doc.page_content,
                "embedding": embedding,
                "source": doc.metadata.get("source", ""),
                "chunk_index": doc.metadata.get("chunk_index", 0)
            }
            if sparse_embeddings and i < len(sparse_embeddings):
                item["sparse_embedding"] = sparse_embeddings[i]
            data.append(item)

        await self.client.insert(
            collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
            data=data
        )
        await self._throttled_flush()
        return len(data)

    async def _ensure_bm25_fitted(self, corpus: List[str]):
        """确保 BM25 模型已就绪（词表来源：磁盘持久化 > 当前语料 fit）。

        词表一旦就绪不再重新 fit：每次重 fit 会改变 idf 词到维度的映射，
        导致历史 sparse 向量与新查询的编码空间不一致（#14）。
        新增语料中的未登录词在编码时被忽略，由 dense 通道覆盖。
        """
        if self.bm25_ef is not None:
            return
        if await asyncio.to_thread(self._load_bm25_vocabulary):
            return
        analyzer = build_default_analyzer(language="zh")
        self.bm25_ef = BM25EmbeddingFunction(analyzer)
        await asyncio.to_thread(self.bm25_ef.fit, corpus)
        await asyncio.to_thread(self._save_bm25_vocabulary)
        logger.info("BM25 词表已在当前语料上 fit 并持久化")

    def _bm25_vocab_path(self) -> str:
        """BM25 词表持久化文件路径（与 collection 名绑定）。"""
        vocab_dir = settings.milvus.MILVUS_BM25_VOCAB_DIR or os.path.join(DATA_DIR, "bm25")
        safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", settings.milvus.MILVUS_COLLECTION_NAME)
        return os.path.join(vocab_dir, f"bm25_{safe_name}.json")

    def _load_bm25_vocabulary(self) -> bool:
        """尝试从磁盘加载与 collection 绑定的 BM25 词表。

        Returns:
            bool: 加载成功返回 True；未启用 sparse / 文件不存在 / 文件损坏返回 False，
                  此时 bm25_ef 为 None，由首次插入语料 fit 重建。
        """
        if not self._sparse_enabled:
            return False
        path = self._bm25_vocab_path()
        if not os.path.exists(path):
            logger.info(f"BM25 词表文件不存在，将在首次插入语料时 fit: {path}")
            return False
        try:
            self.bm25_ef = BM25EmbeddingFunction(build_default_analyzer(language="zh"))
            self.bm25_ef.load(path)
            logger.info(f"BM25 词表已从磁盘加载: {path}")
            return True
        except Exception as e:
            logger.warning(
                f"加载 BM25 词表失败，将回退为首次插入时 fit；"
                f"若集合内已有历史 sparse 向量，建议重建以保证编码空间一致: {e}"
            )
            self.bm25_ef = None
            return False

    def _save_bm25_vocabulary(self) -> None:
        """将当前 BM25 词表原子写入磁盘（先写临时文件再替换）。"""
        path = self._bm25_vocab_path()
        tmp_path = f"{path}.tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self.bm25_ef.save(tmp_path)
            os.replace(tmp_path, path)
        except Exception as e:
            logger.warning(f"BM25 词表持久化失败（不影响本次插入，重启后将重新 fit）: {e}")

    def _remove_bm25_vocabulary(self) -> None:
        """删除磁盘上的持久化 BM25 词表（集合重建时词表随之重置）。"""
        path = self._bm25_vocab_path()
        try:
            if os.path.exists(path):
                os.remove(path)
                logger.info(f"集合新建，已重置持久化 BM25 词表: {path}")
        except Exception as e:
            logger.warning(f"重置 BM25 词表文件失败: {e}")

    @staticmethod
    def _convert_sparse_embeddings(sparse_matrix):
        """将 scipy csr_array 转换为 Milvus 接受的 sparse vector 字典列表。

        Returns:
            list[dict]: 每个元素形如 {index: value, ...}
        """
        result = []
        for row in sparse_matrix:
            # 兼容 csr_array 和不同维度的输入
            if hasattr(row, "toarray"):
                arr = row.toarray().flatten()
            else:
                arr = row.flatten() if hasattr(row, "flatten") else row
            vec = {int(idx): float(val) for idx, val in enumerate(arr) if val != 0}
            result.append(vec)
        return result

    async def search(self, query, k=3, document_ids=None, kb_ids=None):
        """基于向量相似度检索相关切片（兼容旧接口，等价于 search_dense）。

        Args:
            query: 用户查询文本。
            k: 返回结果数量上限。
            document_ids: 可选，限定只在这些文档中检索。
            kb_ids: 可选，限定只在这些知识库中检索。

        Returns:
            list[dict]: 包含 content/document_id/kb_id/source/score 等字段的结果列表。
        """
        return await self.search_dense(query, k=k, document_ids=document_ids, kb_ids=kb_ids)

    async def search_dense(self, query, k=3, document_ids=None, kb_ids=None):
        """Dense 向量检索通道。"""
        query_embedding = await self.embeddings.aembed_query(query)

        search_params = {
            "metric_type": "IP",
            "params": {"ef": settings.milvus.MILVUS_EF}
        }
        filter_expr = self._build_filter_expr(document_ids, kb_ids)

        results = await self.client.search(
            collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
            data=[query_embedding],
            anns_field="embedding",
            search_params=search_params,
            limit=k,
            filter=filter_expr,
            output_fields=["kb_id", "document_id", "content", "source", "chunk_index"]
        )
        return self._parse_search_results(results)

    async def search_sparse(self, query, k=3, document_ids=None, kb_ids=None):
        """Sparse BM25 关键词检索通道。

        若 BM25 未启用或初始化失败，返回空列表并记录日志。
        """
        if not self._sparse_enabled or not self.bm25_ef:
            logger.debug("BM25 sparse 检索未启用或未初始化，返回空结果")
            return []

        try:
            query_sparse = await asyncio.to_thread(self.bm25_ef.encode_queries, [query])
            query_sparse = self._convert_sparse_embeddings(query_sparse)
            if not query_sparse:
                return []
        except Exception as e:
            logger.warning(f"BM25 查询编码失败: {e}")
            return []

        search_params = {
            "metric_type": "IP",
            "params": {"drop_ratio_search": 0.2}
        }
        filter_expr = self._build_filter_expr(document_ids, kb_ids)

        try:
            results = await self.client.search(
                collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
                data=query_sparse,
                anns_field="sparse_embedding",
                search_params=search_params,
                limit=k,
                filter=filter_expr,
                output_fields=["kb_id", "document_id", "content", "source", "chunk_index"]
            )
            return self._parse_search_results(results)
        except Exception as e:
            logger.warning(f"Sparse 检索失败: {e}")
            return []

    async def search_hybrid(self, query, k=3, document_ids=None, kb_ids=None):
        """混合检索入口：dense + sparse，返回经 RRF 融合与 Cross-Encoder 重排序后的结果。

        Args:
            query: 用户查询文本。
            k: 最终返回结果数量上限。
            document_ids: 可选，限定只在这些文档中检索。
            kb_ids: 可选，限定只在这些知识库中检索。

        Returns:
            list[dict]: 包含额外字段 `dense_score` / `sparse_score` / `rrf_score` / `rerank_score` 的结果列表。
        """
        from src.services.hybrid_search import reciprocal_rank_fusion, rerank_results

        top_k = max(k, settings.processing.KB_HYBRID_SEARCH_TOP_K)

        dense_results = await self.search_dense(query, k=top_k, document_ids=document_ids, kb_ids=kb_ids)
        sparse_results = await self.search_sparse(query, k=top_k, document_ids=document_ids, kb_ids=kb_ids)

        fused = reciprocal_rank_fusion(
            {"dense": dense_results, "sparse": sparse_results},
            k=settings.processing.KB_RRF_K,
        )

        reranked = await rerank_results(query, fused, top_k=k)
        return reranked

    # 过滤表达式仅接受 UUID 格式的 ID（防御表达式注入的兜底校验）
    _UUID_RE = re.compile(
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    )

    @classmethod
    def _safe_id(cls, value: Any) -> str:
        """校验 ID 为 UUID 后返回其字符串形式，非法输入直接拒绝。

        Milvus filter 表达式无参数化机制，ID 一律先经此白名单校验再拼接，
        防止构造恶意字符串篡改过滤语义。

        Raises:
            ValueError: ID 不符合 UUID 格式。
        """
        v = str(value)
        if not cls._UUID_RE.match(v):
            raise ValueError(f"非法 ID，已拒绝进入过滤表达式: {v[:64]}")
        return v

    @classmethod
    def _build_filter_expr(cls, document_ids=None, kb_ids=None):
        """根据 document_ids 与 kb_ids 构建 Milvus filter 表达式。"""
        conditions = []
        if kb_ids and len(kb_ids) > 0:
            conditions.append(f"kb_id in [{','.join([f'\"{cls._safe_id(kb)}\"' for kb in kb_ids])}]")
        if document_ids and len(document_ids) > 0:
            conditions.append(f"document_id in [{','.join([f'\"{cls._safe_id(doc_id)}\"' for doc_id in document_ids])}]")
        return " && ".join(conditions) if conditions else None

    @staticmethod
    def _parse_search_results(results):
        """统一解析 Milvus search 返回结果。"""
        docs = []
        if not results or not results[0]:
            return docs
        for hit in results[0]:
            doc = {
                "id": hit["id"],
                "kb_id": hit["entity"].get("kb_id"),
                "document_id": hit["entity"].get("document_id"),
                "content": hit["entity"].get("content"),
                "source": hit["entity"].get("source"),
                "chunk_index": hit["entity"].get("chunk_index"),
                "score": hit["distance"]
            }
            docs.append(doc)
        return docs

    async def delete_by_document_id(self, document_id):
        """按文档 ID 删除其所有切片。"""
        expr = f"document_id == '{self._safe_id(document_id)}'"
        await self.client.delete(
            collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
            filter=expr
        )
        await self._throttled_flush()

    async def delete_by_kb_id(self, kb_id):
        """按知识库 ID 删除其下所有切片。"""
        expr = f"kb_id == '{self._safe_id(kb_id)}'"
        await self.client.delete(
            collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
            filter=expr
        )
        await self._throttled_flush()

    async def delete_by_kb_ids(self, kb_ids: List[str]):
        """按多个知识库 ID 批量删除切片，并在最后统一 flush 一次。

        用于批量删除知识库场景，避免每个知识库单独 flush 触发
        Milvus 单集合 0.1 QPS 限流。
        """
        if not kb_ids:
            return
        expr = "kb_id in [" + ",".join(f"'{self._safe_id(kb_id)}'" for kb_id in kb_ids) + "]"
        await self.client.delete(
            collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
            filter=expr
        )
        await self._throttled_flush()

    async def flush(self):
        """显式触发 Milvus flush，受客户端限速保护。"""
        await self._throttled_flush()

    async def get_document_chunks(self, document_id):
        """获取指定文档的所有切片内容。"""
        expr = f"document_id == '{self._safe_id(document_id)}'"
        results = await self.client.query(
            collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
            filter=expr,
            output_fields=["id", "kb_id", "content", "chunk_index", "source"]
        )
        return results

    async def count(self):
        """返回集合中切片总数。"""
        results = await self.client.query(
            collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
            filter="",
            output_fields=["count(*)"]
        )
        return results[0]["count(*)"]

    async def count_by_kb(self, kb_id):
        """返回指定知识库下的切片数量。"""
        expr = f"kb_id == '{self._safe_id(kb_id)}'"
        results = await self.client.query(
            collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
            filter=expr,
            output_fields=["count(*)"]
        )
        return results[0]["count(*)"]

    async def _throttled_flush(self):
        """受限速保护的 flush 调用。

        使用 AsyncLimiter 控制单集合 flush 频率，并在触发 Milvus
        rate limit 时进行指数退避重试，避免客户端默认 75 次重试
        导致接口长时间挂起。
        """
        async def _flush_with_backoff():
            collection_name = settings.milvus.MILVUS_COLLECTION_NAME
            max_retry = settings.milvus.MILVUS_FLUSH_MAX_RETRY
            base_wait = settings.milvus.MILVUS_FLUSH_BASE_WAIT

            for attempt in range(max_retry + 1):
                try:
                    return await self.client.flush(collection_name=collection_name)
                except MilvusException as exc:
                    if attempt >= max_retry or not self._is_rate_limit_error(exc):
                        raise
                    wait = base_wait * (2 ** attempt)
                    logger.warning(
                        "Milvus flush 触发 rate limit，第 %d 次重试，等待 %.1fs: %s",
                        attempt + 1,
                        wait,
                        exc,
                    )
                    await asyncio.sleep(wait)

        try:
            async with self._flush_limiter:
                return await _flush_with_backoff()
        except ValueError as exc:
            # 兜底：限流器配置异常（如 max_rate < 1）时不阻塞业务，记录错误后继续 flush。
            logger.error("flush 限流器配置异常，已跳过限流: %s", exc)
            return await _flush_with_backoff()

    @staticmethod
    def _is_rate_limit_error(exc: MilvusException) -> bool:
        """判断异常是否为 Milvus rate limit 错误。"""
        code = getattr(exc, "code", None)
        message = str(exc).lower()
        # code=8 为 Milvus 限流错误码；同时兜底匹配常见关键字
        return code == 8 or "rate limit" in message or "ratelimiter" in message

    async def drop_collection(self):
        """删除当前集合（慎用）。"""
        has_collection = await self.client.has_collection(settings.milvus.MILVUS_COLLECTION_NAME)
        if has_collection:
            await self.client.drop_collection(settings.milvus.MILVUS_COLLECTION_NAME)

    async def reconnect(self):
        """重新建立 Milvus 连接并确保集合就绪。"""
        self.client = AsyncMilvusClient(
            uri=f"http://{settings.milvus.MILVUS_HOST}:{settings.milvus.MILVUS_PORT}",
            db_name=settings.milvus.MILVUS_DATABASE
        )
        await self._ensure_collection()
