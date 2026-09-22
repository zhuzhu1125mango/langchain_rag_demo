"""审计日志查询接口（P2-4）。

仅限管理员（require_admin）查询敏感操作记录，按操作/资源类型/操作者/时间过滤。
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import CurrentUser, require_admin
from src.database import get_db
from src.services.audit_service import query_audit_logs

logger = logging.getLogger(__name__)

router = APIRouter(tags=["audit"])


def _parse_dt(value: str) -> datetime:
    """解析 ISO 时间字符串为带时区的 datetime；失败抛 422。"""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"时间格式无效: {value}") from e


@router.get("/audit")
async def list_audit_logs(
    action: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    user_id: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_admin),
):
    """分页查询审计日志（需管理员）。"""
    if page < 1 or not (1 <= page_size <= 200):
        raise HTTPException(status_code=422, detail="分页参数非法")
    start_dt = _parse_dt(start) if start else None
    end_dt = _parse_dt(end) if end else None
    if start_dt and end_dt and start_dt > end_dt:
        raise HTTPException(status_code=422, detail="start 不能晚于 end")

    rows, total = await query_audit_logs(
        db,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        user_id=user_id,
        start=start_dt,
        end=end_dt,
        limit=page_size,
        offset=(page - 1) * page_size,
    )

    items = []
    for r in rows:
        items.append(
            {
                "id": r.id,
                "user_id": r.user_id,
                "username": r.username,
                "action": r.action,
                "resource_type": r.resource_type,
                "resource_id": r.resource_id,
                "detail": r.detail,
                "ip_address": r.ip_address,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
        )
    return {"total": total, "page": page, "page_size": page_size, "items": items}