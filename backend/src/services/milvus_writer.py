"""Milvus 写入路径：向量插入、BM25 词表与限速 flush（CODE-1 拆分）。"""

import asyncio
import logging
import os
import re
import uuid
from typing import List

from pymilvus.exceptions import MilvusException

from src.config import DATA_DIR, settings
from src.services.milvus_common import (
    BM25EmbeddingFunction,
    build_default_analyzer,
)

logger = logging.getLogger("milvus_service")


class WriteMixin:
    """向量写入、稀疏编码与 flush 限速，由 MilvusService 组合。"""

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
        # A1：整篇一次性 aembed_documents 在超大 batch 下易 OOM/超时；改为分片 + 限并发
        # （Semaphore 2）跑子批 aembed_documents，降低峰值并可能提速。aembed 返回与输入同序，
        # 按子批 flat 回原序，保证下游 zip(documents, embedding) 一一对应。
        _EMBED_SUB_BATCH = 32
        embeddings = self._get_embeddings()
        if len(contents) > _EMBED_SUB_BATCH:
            _sem = asyncio.Semaphore(2)

            async def _embed_sub(batch):
                async with _sem:
                    return await embeddings.aembed_documents(batch)

            _sub_lists = await asyncio.gather(
                *(_embed_sub(contents[i:i + _EMBED_SUB_BATCH]) for i in range(0, len(contents), _EMBED_SUB_BATCH))
            )
            embeddings_list = [v for _sl in _sub_lists for v in _sl]
        else:
            embeddings_list = await embeddings.aembed_documents(contents)

        # 阶段一：计算 BM25 sparse embedding
        sparse_embeddings = None
        if self._sparse_enabled and documents:
            try:
                await self._ensure_bm25_fitted(contents)
                sparse_embeddings = await asyncio.to_thread(self.bm25_ef.encode_documents, contents)
                # B2：csr->dict 转换为纯 CPU 密集，移入线程池避免阻塞事件循环
                sparse_embeddings = await asyncio.to_thread(self._convert_sparse_embeddings, sparse_embeddings)
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
            if self._has_heading_path:
                item["heading_path"] = doc.metadata.get("heading_path", "")
            if self._has_source_kind:
                # P2 LLM-Wiki：chunk 来源类型，未标注一律按原始文档处理
                item["source_kind"] = doc.metadata.get("source_kind", "raw")
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

    async def flush(self):
        """显式触发 Milvus flush，受客户端限速保护。"""
        await self._throttled_flush()

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
