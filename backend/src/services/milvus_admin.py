"""Milvus 管理操作：按文档/知识库删除、计数与重连（CODE-1 拆分）。"""

import logging
from typing import List

from pymilvus import AsyncMilvusClient
from pymilvus.exceptions import MilvusException

from src.config import settings

logger = logging.getLogger("milvus_service")


class AdminMixin:
    """删除、统计与连接维护，由 MilvusService 组合。"""

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

    async def get_document_chunks(self, document_id):
        """获取指定文档的所有切片内容。"""
        expr = f"document_id == '{self._safe_id(document_id)}'"
        results = await self.client.query(
            collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
            filter=expr,
            output_fields=self._base_output_fields()
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
