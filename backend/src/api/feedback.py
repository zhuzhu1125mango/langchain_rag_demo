"""
评价反馈API - Feedback API

提供问答评价反馈的操作接口：
1. 提交评价（点赞/差评）
2. 获取评价列表
3. 获取评价统计
4. 获取单条评价详情
5. 删除评价
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from pydantic import BaseModel
from typing import List, Optional
import uuid
import logging
from src.database import get_db
from src.auth import get_current_user, CurrentUser, require_owner
from src.models import Feedback, Session as SessionModel
from src.services.learning_engine import learning_engine

logger = logging.getLogger("feedback")

router = APIRouter(prefix="/feedback", tags=["feedback"])


class FeedbackResponse(BaseModel):
    """评价响应数据模型"""
    id: str
    session_id: str
    message_id: str
    rating: int
    reason: str
    created_at: str


class FeedbackCreate(BaseModel):
    """评价创建请求数据模型"""
    session_id: str
    message_id: str
    rating: int
    reason: str = ""


class FeedbackStats(BaseModel):
    """评价统计数据模型"""
    total_count: int
    positive_count: int
    negative_count: int
    average_rating: float


@router.post("/")
async def create_feedback(
    data: FeedbackCreate,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    提交评价反馈

    仅允许为当前用户拥有的会话提交评价，并将反馈 owner_id 绑定到当前用户。

    Args:
        data: 评价数据（会话ID、消息ID、评分、原因）
            - rating: 评分（1表示差评，2表示一般，3表示好评）或使用布尔值（1表示差评，5表示好评）
        db: 数据库会话
        current_user: 当前认证用户

    Returns:
        dict: {"id": 评价ID, "message": "评价提交成功"}
    """
    if data.rating not in [1, 2, 3, 4, 5]:
        raise HTTPException(status_code=400, detail="无效的评分值，评分范围为1-5")

    try:
        session_id_uuid = uuid.UUID(data.session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的会话ID")

    result = await db.execute(select(SessionModel).filter(SessionModel.id == session_id_uuid))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    require_owner(session.user_id, current_user)

    feedback = Feedback(
        session_id=session_id_uuid,
        message_id=data.message_id,
        owner_id=current_user.user_id,
        rating=data.rating,
        reason=data.reason,
    )
    db.add(feedback)
    await db.commit()
    await db.refresh(feedback)

    # 映射 rating(1-5) 到 feedback_score(-1.0 ~ 1.0)，联动学习引擎
    feedback_score = (data.rating - 3) / 2.0
    try:
        execution_id = None
        if session.messages:
            for msg in session.messages:
                if msg.get("id") == data.message_id:
                    execution_id = msg.get("execution_id")
                    break
        if execution_id:
            await learning_engine.record_feedback(
                execution_id=execution_id,
                feedback_score=feedback_score,
                reason=data.reason
            )
            logger.info(f"学习反馈已记录: execution_id={execution_id}, score={feedback_score}")
    except Exception as e:
        logger.error(f"记录学习反馈失败: {e}", exc_info=True)

    return {"id": str(feedback.id), "message": "评价提交成功"}


@router.get("/", response_model=List[FeedbackResponse])
async def list_feedback(
    session_id: Optional[str] = None,
    message_id: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    获取当前用户的评价列表

    Args:
        session_id: 会话ID（可选，用于筛选特定会话的评价）
        message_id: 消息ID（可选，用于筛选特定消息的评价）
        skip: 跳过数量（分页参数）
        limit: 返回数量（分页参数）
        db: 数据库会话
        current_user: 当前认证用户

    Returns:
        list: 评价列表
    """
    query = select(Feedback).filter(Feedback.owner_id == current_user.user_id)

    if session_id:
        try:
            query = query.filter(Feedback.session_id == uuid.UUID(session_id))
        except ValueError:
            raise HTTPException(status_code=400, detail="无效的会话ID")

    if message_id:
        query = query.filter(Feedback.message_id == message_id)

    result = await db.execute(query.order_by(Feedback.created_at.desc()).offset(skip).limit(limit))
    feedbacks = result.scalars().all()

    return [
        FeedbackResponse(
            id=str(f.id),
            session_id=str(f.session_id),
            message_id=f.message_id,
            rating=f.rating,
            reason=f.reason,
            created_at=f.created_at.isoformat()
        ) for f in feedbacks
    ]


@router.get("/stats", response_model=FeedbackStats)
async def get_feedback_stats(
    session_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    获取当前用户的评价统计

    Args:
        session_id: 会话ID（可选，用于筛选特定会话的统计）
        db: 数据库会话
        current_user: 当前认证用户

    Returns:
        FeedbackStats: 评价统计信息
    """
    where_clause = [Feedback.owner_id == current_user.user_id]

    if session_id:
        try:
            where_clause.append(Feedback.session_id == uuid.UUID(session_id))
        except ValueError:
            raise HTTPException(status_code=400, detail="无效的会话ID")

    count_query = select(func.count(Feedback.id)).filter(*where_clause)
    count_result = await db.execute(count_query)
    total_count = count_result.scalar_one()

    if total_count == 0:
        return FeedbackStats(
            total_count=0,
            positive_count=0,
            negative_count=0,
            average_rating=0.0
        )

    positive_query = select(func.count(Feedback.id)).filter(
        Feedback.rating >= 4, *where_clause
    )
    positive_result = await db.execute(positive_query)
    positive_count = positive_result.scalar_one()

    negative_query = select(func.count(Feedback.id)).filter(
        Feedback.rating <= 2, *where_clause
    )
    negative_result = await db.execute(negative_query)
    negative_count = negative_result.scalar_one()

    avg_query = select(func.avg(Feedback.rating)).filter(*where_clause)
    avg_result = await db.execute(avg_query)
    avg_rating = avg_result.scalar_one()

    return FeedbackStats(
        total_count=total_count,
        positive_count=positive_count,
        negative_count=negative_count,
        average_rating=round(float(avg_rating), 2) if avg_rating else 0.0
    )


@router.get("/{feedback_id}", response_model=FeedbackResponse)
async def get_feedback(
    feedback_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    获取单条评价详情

    Args:
        feedback_id: 评价ID
        db: 数据库会话
        current_user: 当前认证用户

    Returns:
        FeedbackResponse: 评价详情
    """
    try:
        result = await db.execute(select(Feedback).filter(Feedback.id == uuid.UUID(feedback_id)))
        feedback = result.scalar_one_or_none()
        if not feedback:
            raise HTTPException(status_code=404, detail="评价不存在")
        require_owner(feedback.owner_id, current_user)

        return FeedbackResponse(
            id=str(feedback.id),
            session_id=str(feedback.session_id),
            message_id=feedback.message_id,
            rating=feedback.rating,
            reason=feedback.reason,
            created_at=feedback.created_at.isoformat()
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的评价ID")


@router.delete("/{feedback_id}")
async def delete_feedback(
    feedback_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    删除评价

    Args:
        feedback_id: 评价ID
        db: 数据库会话
        current_user: 当前认证用户

    Returns:
        dict: {"message": "评价删除成功"}
    """
    try:
        result = await db.execute(select(Feedback).filter(Feedback.id == uuid.UUID(feedback_id)))
        feedback = result.scalar_one_or_none()
        if not feedback:
            raise HTTPException(status_code=404, detail="评价不存在")
        require_owner(feedback.owner_id, current_user)

        await db.delete(feedback)
        await db.commit()
        return {"message": "评价删除成功"}
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的评价ID")


@router.post("/like")
async def like_message(
    message_id: str,
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    点赞消息（快捷接口）

    Args:
        message_id: 消息ID
        session_id: 会话ID
        db: 数据库会话
        current_user: 当前认证用户

    Returns:
        dict: {"id": 评价ID, "message": "点赞成功"}
    """
    try:
        session_id_uuid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的会话ID")

    result = await db.execute(select(SessionModel).filter(SessionModel.id == session_id_uuid))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    require_owner(session.user_id, current_user)

    feedback = Feedback(
        session_id=session_id_uuid,
        message_id=message_id,
        owner_id=current_user.user_id,
        rating=5,
        reason="点赞"
    )
    db.add(feedback)
    await db.commit()
    await db.refresh(feedback)
    return {"id": str(feedback.id), "message": "点赞成功"}


@router.post("/dislike")
async def dislike_message(
    message_id: str,
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    差评消息（快捷接口）

    Args:
        message_id: 消息ID
        session_id: 会话ID
        db: 数据库会话
        current_user: 当前认证用户

    Returns:
        dict: {"id": 评价ID, "message": "差评已记录"}
    """
    try:
        session_id_uuid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的会话ID")

    result = await db.execute(select(SessionModel).filter(SessionModel.id == session_id_uuid))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    require_owner(session.user_id, current_user)

    feedback = Feedback(
        session_id=session_id_uuid,
        message_id=message_id,
        owner_id=current_user.user_id,
        rating=1,
        reason="差评"
    )
    db.add(feedback)
    await db.commit()
    await db.refresh(feedback)
    return {"id": str(feedback.id), "message": "差评已记录"}
