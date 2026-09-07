"""链路追踪查询 API - Trace API（P1-2 全链路 Trace 可视化）

提供单次问答链路的查询接口：
1. GET /api/traces          — 分页列表（按当前用户隔离，支持会话/时间范围过滤）
2. GET /api/traces/{trace_id} — 链路详情（分阶段耗时、token 用量、检索结果等）

隔离规则：JWT/API Key 用户只能看到 user_id 与自身匹配的记录；
开发模式默认用户（user_id="default"）额外可见 user_id 为 NULL 的历史记录。
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.auth import get_current_user, CurrentUser
from src.models.request_trace import RequestTrace

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/traces", tags=["traces"])

# 列表返回的问题预览长度
_QUESTION_PREVIEW_LEN = 120


def _trace_user_filter(current_user: CurrentUser):
    """按当前用户构造 Trace 归属过滤条件。

    默认用户（开发模式匿名）可看到 user_id 为 NULL 的历史记录；
    JWT 用户只能看到自己的记录。
    """
    if current_user.user_id == "default":
        from sqlalchemy import or_

        return or_(RequestTrace.user_id.is_(None), RequestTrace.user_id == "default")
    return RequestTrace.user_id == current_user.user_id


def _serialize_summary(trace: RequestTrace) -> dict:
    """列表项摘要字段（不含大体积 JSON）。"""
    return {
        "id": trace.id,
        "session_id": trace.session_id,
        "question": (trace.question or "")[:_QUESTION_PREVIEW_LEN],
        "primary_mode": trace.primary_mode,
        "fallback_triggered": bool(trace.fallback_triggered),
        "total_latency_ms": trace.total_latency_ms,
        "stage_count": len(trace.stages) if trace.stages else 0,
        "token_usage": trace.token_usage,
        "created_at": trace.created_at.isoformat() if trace.created_at else None,
    }


@router.get("")
async def list_traces(
    session_id: Optional[str] = Query(None, description="按会话 ID 过滤"),
    start: Optional[datetime] = Query(None, description="起始时间（含）"),
    end: Optional[datetime] = Query(None, description="结束时间（含）"),
    limit: int = Query(20, ge=1, le=100, description="每页数量"),
    offset: int = Query(0, ge=0, description="偏移量"),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """查询当前用户的链路追踪列表（按创建时间倒序分页）。"""
    conditions = [_trace_user_filter(current_user)]
    if session_id:
        conditions.append(RequestTrace.session_id == session_id)
    if start:
        conditions.append(RequestTrace.created_at >= start)
    if end:
        conditions.append(RequestTrace.created_at <= end)

    total = await db.scalar(select(func.count()).select_from(RequestTrace).where(*conditions))
    result = await db.execute(
        select(RequestTrace)
        .where(*conditions)
        .order_by(RequestTrace.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    items = [_serialize_summary(t) for t in result.scalars().all()]

    return {"total": int(total or 0), "items": items}


@router.get("/{trace_id}")
async def get_trace(
    trace_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """查询单条链路追踪详情（跨用户访问返回 404，避免泄露存在性）。"""
    trace = await db.get(RequestTrace, trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="链路记录不存在")

    owner_id = trace.user_id
    is_default_user = current_user.user_id == "default"
    if owner_id and owner_id != current_user.user_id:
        raise HTTPException(status_code=404, detail="链路记录不存在")
    if owner_id is None and not is_default_user:
        raise HTTPException(status_code=404, detail="链路记录不存在")

    return {
        "id": trace.id,
        "session_id": trace.session_id,
        "user_id": trace.user_id,
        "question": trace.question,
        "resolved_question": trace.resolved_question,
        "intent_decision": trace.intent_decision,
        "primary_mode": trace.primary_mode,
        "tool_calls": trace.tool_calls,
        "search_results": trace.search_results,
        "kb_results": trace.kb_results,
        "stages": trace.stages,
        "token_usage": trace.token_usage,
        "fallback_triggered": bool(trace.fallback_triggered),
        "fallback_reason": trace.fallback_reason,
        "output_pollution_detected": bool(trace.output_pollution_detected),
        "final_answer": trace.final_answer,
        "total_latency_ms": trace.total_latency_ms,
        "created_at": trace.created_at.isoformat() if trace.created_at else None,
    }
