"""Agent 跨请求记忆的向量生命周期（CODE-2 一致性链路）。

记忆切片写入 Milvus 时 ``kb_id`` 固定为 :data:`MEMORY_KB_ID`（``"memory"``，不是
UUID），``document_id`` 为 ``uuid5("agent-memory:{session_id}")``。这两点是写入侧
与删除侧必须共享的约定——一旦某侧独自改动，删除就会「看起来成功、实际什么都没删」。

历史缺陷：会话删除 / 账号注销只删 DB 行，从不碰这些切片，于是用户以为「对话已
删除」，摘要却永久留在向量库里可被直接 query 出来（隐私承诺失效），同时集合无限
增长。本模块把 doc_id 生成与按会话清理收敛到一处，两侧共用。
"""

import logging
import uuid
from typing import Iterable, List

from src.utils.error_handling import ErrorSeverity, log_exception

logger = logging.getLogger("rag_system")

# 记忆切片的 kb_id 取值：故意不是 UUID，避免与真实知识库混淆；
# 也正因为它不是 UUID，`delete_by_kb_ids(["memory"])` 会被 _safe_id 白名单拒绝，
# 所以记忆只能按 document_id 清理，不能按「库」清理。
MEMORY_KB_ID = "memory"

_MEMORY_NAMESPACE_PREFIX = "agent-memory:"

# 单次 delete 表达式的 id 数量上限：表达式过长会被 Milvus 拒绝（且难以排查）
_DELETE_CHUNK = 100


def memory_doc_id(session_id: str) -> str:
    """为跨请求记忆生成确定性 document_id（UUIDv5，基于 session_id）。

    同会话得到同一 document_id，使记忆切片聚合可统一检索；不同会话 UUID 不同，
    天然隔离。必须产出合法 UUID——Milvus 过滤表达式 ``_safe_id`` 仅放行 UUID，
    拒绝 ``memory:{session_id}`` 这类带冒号的字符串（防注入白名单）。
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{_MEMORY_NAMESPACE_PREFIX}{session_id}"))


def _chunks(items: List[str], size: int = _DELETE_CHUNK) -> Iterable[List[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


async def delete_session_memories(
    session_ids: Iterable[str],
    *,
    action: str,
    **context,
) -> int:
    """删除若干会话的记忆向量，返回**尝试清理**的会话数。

    设计取舍：记忆清理不阻断会话/账号删除（用户点了删除就必须删掉主记录），
    但残留属于隐私数据，因此失败固定按 ``REPORTABLE`` 记录——这不是「上游抖动
    等下次重试就好」，而是「元数据已删、向量再也无人认领」。
    """
    ids = [str(s) for s in session_ids if s]
    if not ids:
        return 0

    doc_ids = [memory_doc_id(s) for s in ids]
    try:
        from src.services.vector_store import VectorStoreManager

        vector_store = await VectorStoreManager.get_instance()
        for batch in _chunks(doc_ids):
            await vector_store.delete_by_document_ids(batch)
    except Exception as exc:
        log_exception(
            logger,
            exc,
            action=action,
            severity=ErrorSeverity.REPORTABLE,
            message="会话记忆向量清理失败：对话摘要仍留在向量库（会话元数据已删除）",
            sessions=len(ids),
            session_ids=",".join(ids[:5]),
            **context,
        )
        return 0

    logger.info("会话记忆向量已清理: sessions=%d action=%s", len(ids), action)
    return len(ids)
