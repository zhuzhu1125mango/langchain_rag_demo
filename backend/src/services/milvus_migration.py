"""Milvus 集合迁移：影子集合重建、原子切换与回滚（CODE-1 拆分）。"""

import asyncio
import logging
import time
import uuid
from typing import Any, Dict, List, Optional


from src.config import settings

logger = logging.getLogger("milvus_service")


class MigrationMixin:
    """Embedding 迁移相关的集合重建与切换，由 MilvusService 组合。"""

    async def rebuild_collection(
        self,
        target_dimension: int,
        embeddings: Any,
        progress_cb: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """把现有集合内容重新向量化写入影子集合（P3）。

        只读取标量字段并复用现有 chunk 文本，不重新解析源文件；sparse 向量复用
        与 collection 绑定的 BM25 词表重新编码，保持查询编码空间一致。
        成功后返回影子集合名，由 ``swap_collection`` 原子替换。
        """
        live = settings.milvus.MILVUS_COLLECTION_NAME
        shadow = f"{live}__rebuild_{int(time.time())}_{uuid.uuid4().hex[:4]}"
        has_live = await self.client.has_collection(live)
        total = await self._collection_count(live) if has_live else 0

        schema = await self._build_schema(dimension=target_dimension)
        await self.client.create_collection(collection_name=shadow, schema=schema)

        inserted = 0
        skipped = 0
        if has_live and total:
            info = await self.client.describe_collection(live)
            available = {f["name"] for f in info.get("fields", [])}
            output_fields = [
                name
                for name in ("id", "kb_id", "document_id", "content", "source", "chunk_index", "heading_path", "source_kind")
                if name in available
            ]
            batch_size = 500
            offset = 0
            while offset < total:
                rows = await self.client.query(
                    collection_name=live,
                    filter="",
                    output_fields=output_fields,
                    limit=batch_size,
                    offset=offset,
                )
                if not rows:
                    break
                offset += len(rows)
                usable = [row for row in rows if str(row.get("content") or "").strip()]
                skipped += len(rows) - len(usable)
                if not usable:
                    continue
                texts = [str(row["content"]) for row in usable]
                dense_vectors = await embeddings.aembed_documents(texts)
                if dense_vectors and len(dense_vectors[0]) != target_dimension:
                    raise RuntimeError(
                        f"目标 Embedding 维度 {len(dense_vectors[0])} 与声明维度 {target_dimension} 不一致"
                    )
                sparse_vectors = await self._encode_sparse_for_migration(texts)
                data = []
                for index, (row, vector) in enumerate(zip(usable, dense_vectors)):
                    item = {
                        "id": str(row["id"]),
                        "kb_id": row.get("kb_id") or "",
                        "document_id": row.get("document_id") or "",
                        "content": row["content"],
                        "embedding": vector,
                        "source": row.get("source") or "",
                        "chunk_index": int(row.get("chunk_index") or 0),
                        "heading_path": row.get("heading_path") or "",
                        "source_kind": row.get("source_kind") or "raw",
                    }
                    if sparse_vectors and index < len(sparse_vectors):
                        item["sparse_embedding"] = sparse_vectors[index]
                    data.append(item)
                await self.client.insert(collection_name=shadow, data=data)
                inserted += len(data)
                if progress_cb is not None:
                    await progress_cb(inserted, total)

        await self.client.flush(collection_name=shadow)
        await self._create_indexes(collection_name=shadow)
        await self.client.load_collection(shadow)
        logger.info("Embedding 索引影子集合构建完成: %s, chunks=%d, skipped=%d", shadow, inserted, skipped)
        return {
            "shadow_collection": shadow,
            "chunk_count": inserted,
            "source_count": total,
            "skipped": skipped,
        }

    async def _encode_sparse_for_migration(self, texts: List[str]) -> Optional[List[Any]]:
        """用现有 BM25 词表编码；词表缺失时跳过 sparse（dense 仍可用）。"""
        if not self._sparse_enabled:
            return None
        if self.bm25_ef is None:
            await asyncio.to_thread(self._load_bm25_vocabulary)
        if self.bm25_ef is None:
            logger.warning("BM25 词表不可用，重建集合将不写入 sparse 向量")
            return None
        try:
            raw = await asyncio.to_thread(self.bm25_ef.encode_documents, texts)
            return await asyncio.to_thread(self._convert_sparse_embeddings, raw)
        except Exception as exc:
            logger.warning("重建集合时 BM25 编码失败，跳过 sparse: %s", exc)
            return None

    async def swap_collection(self, shadow_collection: str) -> str:
        """把影子集合替换为线上集合，旧集合保留为备份；失败时尝试还原。"""
        live = settings.milvus.MILVUS_COLLECTION_NAME
        backup = f"{live}__backup_{int(time.time())}_{uuid.uuid4().hex[:4]}"
        await self.client.rename_collection(live, backup)
        try:
            await self.client.rename_collection(shadow_collection, live)
            await self.client.load_collection(live)
        except Exception:
            try:
                await self.client.rename_collection(backup, live)
                await self.client.load_collection(live)
            except Exception as restore_exc:  # pragma: no cover - 极端故障路径
                logger.error("索引交换失败且自动还原失败，需人工介入: %s", restore_exc)
            raise
        return backup

    async def restore_collection(self, backup_collection: str) -> str:
        """回滚：把备份集合换回线上，当前集合归档保留。"""
        live = settings.milvus.MILVUS_COLLECTION_NAME
        archive = f"{live}__rolledback_{int(time.time())}_{uuid.uuid4().hex[:4]}"
        if await self.client.has_collection(live):
            await self.client.rename_collection(live, archive)
        await self.client.rename_collection(backup_collection, live)
        await self.client.load_collection(live)
        return archive

    async def drop_collection_by_name(self, collection_name: str) -> None:
        """删除非线上集合（影子/失败副本）；线上集合必须走受控迁移流程。"""
        live = settings.milvus.MILVUS_COLLECTION_NAME
        if collection_name == live:
            raise RuntimeError("拒绝删除线上 Milvus 集合")
        if await self.client.has_collection(collection_name):
            await self.client.drop_collection(collection_name)
