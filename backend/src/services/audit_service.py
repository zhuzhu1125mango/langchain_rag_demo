"""审计日志持久化服务。

record_audit 为 best-effort：审计失败绝不影响主业务流程（捕获一切异常并仅告警）。
敏感操作入口调用方传入操作者、动作、资源与附加上下文，由本服务统一落库。
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import AsyncSessionLocal
from src.models.audit_log import AuditLog

logger = logging.getLogger(__name__)


async def _commit_to_session(db: AsyncSession, record: AuditLog) -> None:
    db.add(record)
    await db.commit()


async def record_audit(
    *,
    action: str,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    detail: Optional[dict] = None,
    actor_user_id: Optional[str] = None,
    actor_username: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> None:
    """记录一条审计日志（异步、best-effort、绝不抛出）。

    Args:
        action: 操作标识，如 "document.upload" / "document.delete" / "kb.create" / "config.update" / "auth.register"。
        resource_type: 资源类型，如 "document" / "knowledge_base" / "config" / "user"。
        resource_id: 资源 ID（UUID 字符串或名称）。
        detail: 结构化附加信息，如文件名、批次数量、变更字段。
        actor_user_id / actor_username: 操作者。
        ip_address: 请求来源 IP。
    """
    try:
        async with AsyncSessionLocal() as db:
            record = AuditLog(
                user_id=actor_user_id,
                username=actor_username,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                detail=detail,
                ip_address=ip_address,
                created_at=datetime.now(timezone.utc),
            )
            await _commit_to_session(db, record)
    except Exception as e:  # noqa: BLE001 —— 审计失败不应阻断主流程
        logger.warning(f"审计日志写入失败（已忽略）: action={action} err={e}")


async def query_audit_logs(
    db: AsyncSession,
    *,
    action: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    user_id: Optional[str] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[AuditLog], int]:
    """分页查询审计日志，返回 (记录列表, 总数)。"""
    conds = []
    if action:
        conds.append(AuditLog.action == action)
    if resource_type:
        conds.append(AuditLog.resource_type == resource_type)
    if resource_id:
        conds.append(AuditLog.resource_id == resource_id)
    if user_id:
        conds.append(AuditLog.user_id == user_id)
    if start:
        conds.append(AuditLog.created_at >= start)
    if end:
        conds.append(AuditLog.created_at <= end)

    count_stmt = select(func.count(AuditLog.id)).where(*conds)
    total = await db.scalar(count_stmt) or 0

    stmt = (
        select(AuditLog)
        .where(*conds)
        .order_by(AuditLog.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows), total