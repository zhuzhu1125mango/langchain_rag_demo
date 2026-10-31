"""用户数据导出与账号注销（SEC-14）。

导出返回可移植 JSON（不含密码哈希、API Key 明文/哈希）；
注销按 owner 级联清理数据库记录，并尽力删除向量与 MinIO 原文件。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import (
    Badcase,
    Document,
    Experiment,
    Feedback,
    KnowledgeBase,
    RequestTrace,
    Session,
    StrategyExecution,
    StrategyFeedback,
    User,
    UserApiKey,
    WikiPage,
)

from src.utils.error_handling import ErrorSeverity, log_exception

logger = logging.getLogger("rag_system")


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value is not None else None)


async def export_user_data(db: AsyncSession, user_id: str) -> Dict[str, Any]:
    """导出指定用户的可携带数据（SEC-14）。"""
    uid = uuid.UUID(user_id)
    user = await db.scalar(select(User).where(User.id == uid))
    if user is None:
        raise ValueError("用户不存在")

    kbs = (await db.execute(select(KnowledgeBase).where(KnowledgeBase.owner_id == user_id))).scalars().all()
    docs = (await db.execute(select(Document).where(Document.owner_id == user_id))).scalars().all()
    sessions = (await db.execute(select(Session).where(Session.user_id == user_id))).scalars().all()
    feedbacks = (await db.execute(select(Feedback).where(Feedback.owner_id == user_id))).scalars().all()
    experiments = (await db.execute(select(Experiment).where(Experiment.owner_id == user_id))).scalars().all()
    wiki_pages = (await db.execute(select(WikiPage).where(WikiPage.owner_id == user_id))).scalars().all()
    traces = (await db.execute(select(RequestTrace).where(RequestTrace.user_id == user_id))).scalars().all()
    badcases = (await db.execute(select(Badcase).where(Badcase.user_id == user_id))).scalars().all()
    executions = (
        await db.execute(select(StrategyExecution).where(StrategyExecution.user_id == user_id))
    ).scalars().all()
    api_keys = (await db.execute(select(UserApiKey).where(UserApiKey.user_id == uid))).scalars().all()

    return {
        "exported_at": _iso(__import__("datetime").datetime.now(__import__("datetime").timezone.utc)),
        "user": {
            "id": str(user.id),
            "username": user.username,
            "created_at": _iso(user.created_at),
        },
        "knowledge_bases": [
            {
                "id": str(kb.id),
                "name": kb.name,
                "description": kb.description,
                "created_at": _iso(kb.created_at),
            }
            for kb in kbs
        ],
        "documents": [
            {
                "id": str(doc.id),
                "filename": doc.filename,
                "file_type": doc.file_type,
                "size": doc.size,
                "kb_id": str(doc.kb_id) if doc.kb_id else None,
                "status": doc.status,
                "created_at": _iso(doc.created_at),
            }
            for doc in docs
        ],
        "sessions": [
            {
                "id": str(s.id),
                "title": s.title,
                "messages": s.messages or [],
                "created_at": _iso(s.created_at),
            }
            for s in sessions
        ],
        "feedbacks": [
            {
                "id": str(f.id),
                "session_id": str(f.session_id) if f.session_id else None,
                "rating": f.rating,
                "reason": f.reason,
                "created_at": _iso(f.created_at),
            }
            for f in feedbacks
        ],
        "experiments": [
            {"id": str(e.id), "name": e.name, "status": e.status, "created_at": _iso(e.created_at)}
            for e in experiments
        ],
        "wiki_pages": [
            {"id": str(p.id), "title": p.title, "created_at": _iso(p.created_at)} for p in wiki_pages
        ],
        "traces": [
            {"id": str(t.id), "question": t.question, "created_at": _iso(t.created_at)} for t in traces
        ],
        "badcases": [
            {"id": str(b.id), "question": b.question, "created_at": _iso(b.created_at)} for b in badcases
        ],
        "strategy_executions": [
            {"id": str(x.id), "question": x.question, "created_at": _iso(x.created_at)} for x in executions
        ],
        "api_keys": [
            {"id": str(k.id), "name": k.name, "prefix": k.key_prefix, "created_at": _iso(k.created_at)}
            for k in api_keys
        ],
    }


async def delete_user_account(db: AsyncSession, user_id: str) -> Dict[str, int]:
    """注销账号并级联清理用户资源（SEC-14）。返回删除计数摘要。"""
    uid = uuid.UUID(user_id)
    summary: Dict[str, int] = {}

    docs = (await db.execute(select(Document).where(Document.owner_id == user_id))).scalars().all()
    kbs = (await db.execute(select(KnowledgeBase).where(KnowledgeBase.owner_id == user_id))).scalars().all()
    # 会话摘要的向量（kb_id="memory"）既不属于任何文档也不属于任何知识库，
    # 只能由会话 ID 反推 document_id；必须在这里先快照，注销后 DB 行就没了。
    session_ids = [
        str(sid)
        for sid in (
            await db.execute(select(Session.id).where(Session.user_id == user_id))
        ).scalars().all()
    ]

    # 向量与对象存储清理：best-effort，失败仅告警，不阻断数据库注销。
    # 但「残留」是永久性的（DB 行删掉后再无任何记录指向这些向量），
    # 所以严重度按后果定：REPORTABLE，而非「等下次重试就好」的 RECOVERABLE。
    try:
        from src.services.vector_store import VectorStoreManager

        vector_store = await VectorStoreManager.get_instance()
        doc_ids = [str(doc.id) for doc in docs]
        if doc_ids:
            try:
                await vector_store.delete_by_document_ids(doc_ids)
            except Exception as exc:
                log_exception(
                    logger, exc,
                    action="account.delete.vectors.document",
                    severity=ErrorSeverity.REPORTABLE,
                    message="注销清理文档向量失败：已注销用户的内容仍留在向量库，可被检索命中",
                    user_id=user_id,
                    documents=len(doc_ids),
                )
        kb_ids = [str(kb.id) for kb in kbs]
        if kb_ids:
            try:
                await vector_store.delete_by_kb_ids(kb_ids)
            except Exception as exc:
                log_exception(
                    logger, exc,
                    action="account.delete.vectors.kb",
                    severity=ErrorSeverity.REPORTABLE,
                    message="注销清理知识库向量失败：已注销用户的内容仍留在向量库，可被检索命中",
                    user_id=user_id,
                    knowledge_bases=len(kb_ids),
                )
    except Exception as exc:
        log_exception(
            logger, exc,
            action="account.delete.vectors.connect",
            severity=ErrorSeverity.REPORTABLE,
            message="注销时向量库不可用，本次注销的全部向量未清理（数据残留）",
            user_id=user_id,
            documents=len(docs),
            knowledge_bases=len(kbs),
        )

    try:
        from src.services.minio_service import MinioService

        minio = await MinioService.get_instance()
        for doc in docs:
            if doc.file_path:
                try:
                    await minio.delete_file_async(doc.file_path)
                except Exception as exc:
                    log_exception(
                        logger, exc,
                        action="account.delete.objects.document",
                        severity=ErrorSeverity.RECOVERABLE,
                        message="注销删除原文件失败，继续其余清理",
                        doc_id=doc.id,
                    )
    except Exception as exc:
        log_exception(
            logger, exc,
            action="account.delete.objects.connect",
            severity=ErrorSeverity.RECOVERABLE,
            message="注销时 MinIO 不可用，跳过原文件清理",
            user_id=user_id,
        )

    execution_ids = select(StrategyExecution.id).where(StrategyExecution.user_id == user_id)
    result = await db.execute(delete(StrategyFeedback).where(StrategyFeedback.execution_id.in_(execution_ids)))
    summary["strategy_feedbacks"] = result.rowcount or 0
    for model, column, label in (
        (Feedback, Feedback.owner_id, "feedbacks"),
        (Experiment, Experiment.owner_id, "experiments"),
        (Session, Session.user_id, "sessions"),
        (Document, Document.owner_id, "documents"),
        (KnowledgeBase, KnowledgeBase.owner_id, "knowledge_bases"),
        (WikiPage, WikiPage.owner_id, "wiki_pages"),
        (RequestTrace, RequestTrace.user_id, "traces"),
        (Badcase, Badcase.user_id, "badcases"),
        (StrategyExecution, StrategyExecution.user_id, "strategy_executions"),
    ):
        result = await db.execute(delete(model).where(column == user_id))
        summary[label] = result.rowcount or 0

    # UserApiKey.user_id 是 UUID 列，需用 UUID 绑定避免类型不匹配
    result = await db.execute(delete(UserApiKey).where(UserApiKey.user_id == uid))
    summary["api_keys"] = result.rowcount or 0

    result = await db.execute(delete(User).where(User.id == uid))
    summary["users"] = result.rowcount or 0
    await db.commit()

    # 会话记忆向量：注销后这批摘要再无任何元数据指向，也无法按库删除
    # （kb_id="memory" 不是 UUID，会被 _safe_id 白名单拒绝），只能按会话清。
    from src.services.agent_memory import delete_session_memories

    await delete_session_memories(
        session_ids, action="account.delete.memory_vectors", user_id=user_id
    )

    # 内容删除后失效语义缓存与知识库列表缓存
    from src.services.semantic_cache_service import schedule_invalidation

    for kb in kbs:
        try:
            schedule_invalidation(str(kb.id))
        except Exception as exc:
            log_exception(
                logger, exc,
                action="account.delete.invalidate_semantic_cache",
                severity=ErrorSeverity.RECOVERABLE,
                message="注销后失效语义缓存失败，旧答案将按 TTL 自然过期",
                kb_id=kb.id,
            )
    try:
        # 注意：invalidate_kb_list_cache 属 api.knowledge_base，不在 services.cache_service；
        # 此前写错模块导致 ImportError 被静默吞掉，注销后列表缓存从未失效。
        from src.api.knowledge_base import invalidate_kb_list_cache

        await invalidate_kb_list_cache(user_id)
    except Exception as exc:
        log_exception(
            logger, exc,
            action="account.delete.invalidate_kb_list_cache",
            severity=ErrorSeverity.RECOVERABLE,
            message="注销后失效知识库列表缓存失败，前端可能短暂显示已删除知识库",
            user_id=user_id,
        )
    return summary
