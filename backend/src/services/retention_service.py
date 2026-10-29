"""数据库日志保留期清扫（SEC-14）。

按天清理过期的请求 trace 与审计日志，避免含用户问题/答案的明细无限期堆积。
生产 stdout 日志的保留由容器平台策略负责，应用文件日志使用按天轮转。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.models import AuditLog, RequestTrace
from src.utils.error_handling import log_exception

logger = logging.getLogger("rag_system")


async def purge_expired_data(db: AsyncSession) -> Dict[str, int]:
    """删除超过保留期的 trace/audit 记录；返回删除计数。"""
    now = datetime.now(timezone.utc)
    trace_cutoff = now - timedelta(days=max(1, int(settings.security.TRACE_RETENTION_DAYS or 30)))
    audit_cutoff = now - timedelta(days=max(1, int(settings.security.AUDIT_RETENTION_DAYS or 365)))

    trace_result = await db.execute(delete(RequestTrace).where(RequestTrace.created_at < trace_cutoff))
    audit_result = await db.execute(delete(AuditLog).where(AuditLog.created_at < audit_cutoff))
    await db.commit()
    summary = {
        "traces": trace_result.rowcount or 0,
        "audit_logs": audit_result.rowcount or 0,
    }
    if summary["traces"] or summary["audit_logs"]:
        logger.info("保留期清扫完成: %s", summary)
    return summary


async def periodic_retention(interval_s: float) -> None:
    """后台周期清扫；任何失败仅告警，不退出循环。"""
    from src.database import async_session_maker

    interval = max(3600.0, float(interval_s or 86400))
    while True:
        await asyncio.sleep(interval)
        try:
            async with async_session_maker() as db:
                await purge_expired_data(db)
        except Exception as exc:
            # 清扫失败只影响本轮：下个周期重试，不退出循环。
            # 分级交给异常性质——连接类记 warning；若是解析/结构类缺陷则升级为 error，
            # 否则「过期审计日志一直没被删」会在 warning 里长期无人处理（SEC-14）
            log_exception(
                logger,
                exc,
                action="retention.sweep",
                message="保留期清扫失败，下个周期重试",
                interval_s=interval,
            )
