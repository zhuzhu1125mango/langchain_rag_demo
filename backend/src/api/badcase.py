"""Badcase 反馈 API。

提供 badcase 提交、查询、统计接口，用于收集问题样本并触发在线学习。
"""

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import CurrentUser, get_current_user
from src.database import get_db
from src.models import Badcase
from src.services.badcase import BadcaseFeedbackHandler, BadcaseCategory
from src.services.badcase.feedback_handler import BadcaseCreateRequest

router = APIRouter(prefix="/badcases", tags=["badcases"])


class BadcaseCreate(BaseModel):
    """Badcase 创建请求。"""

    question: str
    answer: Optional[str] = None
    session_id: Optional[str] = None
    message_id: Optional[str] = None
    feedback_type: str = "negative"
    reason: Optional[str] = None
    retrieved_sources: Optional[List[dict]] = None
    intent_decision: Optional[dict] = None


class BadcaseResponse(BaseModel):
    """Badcase 响应模型。"""

    id: str
    session_id: Optional[str]
    message_id: Optional[str]
    user_id: Optional[str]
    question: str
    answer: Optional[str]
    category: Optional[str]
    severity: int
    feedback_type: str
    reason: Optional[str]
    learning_applied: bool
    created_at: str


class BadcaseStats(BaseModel):
    """Badcase 统计模型。"""

    total_count: int
    category_counts: dict
    severity_avg: float


@router.post("/", response_model=BadcaseResponse)
async def create_badcase(
    data: BadcaseCreate,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """提交一条 badcase 反馈。

    系统会自动分类问题类型，并触发意图路由的在线学习。
    """
    if not data.question:
        raise HTTPException(status_code=400, detail="问题内容不能为空")

    handler = BadcaseFeedbackHandler()
    request = BadcaseCreateRequest(
        question=data.question,
        answer=data.answer,
        session_id=data.session_id,
        message_id=data.message_id,
        user_id=current_user.user_id,
        feedback_type=data.feedback_type,
        reason=data.reason,
        retrieved_sources=data.retrieved_sources,
        intent_decision=data.intent_decision,
    )

    result = await handler.handle(request, db_session=db)

    # 重新查询刚保存的记录以返回完整响应
    if result.get("badcase_id"):
        try:
            record_result = await db.execute(
                select(Badcase).filter(Badcase.id == uuid.UUID(result["badcase_id"]))
            )
            record = record_result.scalar_one_or_none()
            if record:
                return BadcaseResponse(
                    id=str(record.id),
                    session_id=record.session_id,
                    message_id=record.message_id,
                    user_id=record.user_id,
                    question=record.question,
                    answer=record.answer,
                    category=record.category,
                    severity=record.severity,
                    feedback_type=record.feedback_type,
                    reason=record.reason,
                    learning_applied=record.learning_applied,
                    created_at=record.created_at.isoformat() if record.created_at else "",
                )
        except ValueError:
            pass

    # 持久化失败但分类与学习仍完成时，返回简化响应
    return BadcaseResponse(
        id="",
        session_id=data.session_id,
        message_id=data.message_id,
        user_id=current_user.user_id,
        question=data.question,
        answer=data.answer,
        category=result["category"],
        severity=result["severity"],
        feedback_type=data.feedback_type,
        reason=data.reason,
        learning_applied=result.get("learning_result") is not None,
        created_at="",
    )


@router.get("/", response_model=List[BadcaseResponse])
async def list_badcases(
    category: Optional[str] = None,
    feedback_type: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """获取当前用户的 badcase 列表。"""
    query = select(Badcase).filter(Badcase.user_id == current_user.user_id)

    if category:
        query = query.filter(Badcase.category == category)
    if feedback_type:
        query = query.filter(Badcase.feedback_type == feedback_type)

    result = await db.execute(query.order_by(Badcase.created_at.desc()).offset(skip).limit(limit))
    records = result.scalars().all()

    return [
        BadcaseResponse(
            id=str(r.id),
            session_id=r.session_id,
            message_id=r.message_id,
            user_id=r.user_id,
            question=r.question,
            answer=r.answer,
            category=r.category,
            severity=r.severity,
            feedback_type=r.feedback_type,
            reason=r.reason,
            learning_applied=r.learning_applied,
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in records
    ]


@router.get("/stats", response_model=BadcaseStats)
async def get_badcase_stats(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """获取当前用户的 badcase 统计信息。"""
    count_query = select(func.count(Badcase.id)).filter(Badcase.user_id == current_user.user_id)
    count_result = await db.execute(count_query)
    total_count = count_result.scalar_one()

    category_counts = {}
    if total_count > 0:
        category_query = (
            select(Badcase.category, func.count(Badcase.id))
            .filter(Badcase.user_id == current_user.user_id)
            .group_by(Badcase.category)
        )
        category_result = await db.execute(category_query)
        for row in category_result.all():
            category_counts[row[0] or "unknown"] = row[1]

    avg_query = select(func.avg(Badcase.severity)).filter(Badcase.user_id == current_user.user_id)
    avg_result = await db.execute(avg_query)
    severity_avg = avg_result.scalar_one() or 0.0

    return BadcaseStats(
        total_count=total_count,
        category_counts=category_counts,
        severity_avg=round(float(severity_avg), 2),
    )


@router.get("/categories")
async def list_badcase_categories():
    """获取支持的 badcase 分类列表。"""
    return {"categories": [c.value for c in BadcaseCategory]}
