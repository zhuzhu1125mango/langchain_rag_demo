"""聊天消息路由：非流式发送与消息反馈。"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import uuid
import logging
import time
from datetime import datetime, timezone
from src.database import get_db, async_session
from src.auth import get_current_user, CurrentUser, require_owner
from src.models import Session as SessionModel, Feedback
from src.services.rag_chain import RAGChain
from src.services.vector_store import VectorStoreManager
from src.services.session_service import append_session_message
from src.services.learning_engine import learning_engine
from src.services.pipeline.state import set_ab_config, reset_ab_config
from src.services.title_generator import TitleGenerator
from src.utils.sensitive_words import sensitive_filter
from src.utils.validators import parse_uuid_list, validate_kb_ownership

from .common import (
    FeedbackRequest,
    MessageRequest,
    chat_quota_dependency,
    _parse_session_id,
    _record_ab_metric,
    _resolve_ab_config,
)

logger = logging.getLogger("src.api.chat")

router = APIRouter(tags=["chat"])

@router.post("/messages", dependencies=[Depends(chat_quota_dependency)])
async def send_message(
    request: MessageRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    发送消息并获取回答（非流式）

    数据库访问拆分为三段独立短会话（口径同 /stream 端点）：kb 校验、
    会话+用户消息落库、回答落库——LLM 生成（可达数十秒）期间不持有
    DB 连接，避免并发请求饿死连接池。

    Args:
        request: 请求数据（问题、会话ID、知识库ID列表）
        current_user: 当前认证用户

    Returns:
        dict: {"session_id": 会话ID, "answer": 回答内容, "sources": 来源列表, "answer_type": 回答类型}
    """
    # 敏感词检测
    detected, word, category = sensitive_filter.detect(request.question)
    if detected:
        raise HTTPException(status_code=400, detail=f"输入包含敏感内容 [{category}]: {word}")

    # 知识库 ID 校验：格式 + 归属（独立短会话）
    kb_ids = parse_uuid_list(request.kb_ids)
    async with async_session() as db:
        await validate_kb_ownership(db, kb_ids, current_user)

    # 加载向量库和RAG链
    vector_store = await VectorStoreManager.get_instance()
    rag_chain = await RAGChain.get_instance(vector_store)

    # 生成消息ID
    user_message_id = str(uuid.uuid4())
    assistant_message_id = str(uuid.uuid4())

    # 获取或创建会话 + 保存用户消息（短会话 1；生成回答之前保存，保证历史完整）
    # 原子追加：并发写同一会话时由 PG 行锁串行化，不丢消息
    async with async_session() as db:
        if request.session_id:
            result = await db.execute(select(SessionModel).filter(SessionModel.id == _parse_session_id(request.session_id)))
            session = result.scalar_one_or_none()
            if not session:
                raise HTTPException(status_code=404, detail="会话不存在")
            require_owner(session.user_id, current_user)
            history = session.messages.copy()
            session_title = session.title
        else:
            session = SessionModel(
                user_id=current_user.user_id,
                title=request.question[:50],
                messages=[],
                kb_ids=kb_ids,
                updated_at=datetime.now(timezone.utc)
            )
            db.add(session)
            await db.commit()
            await db.refresh(session)
            history = []
            session_title = session.title
        session_uuid = session.id

        user_msg_count = await append_session_message(db, session_uuid, {
            "id": user_message_id,
            "role": "user",
            "content": request.question,
            "timestamp": datetime.now().isoformat()
        })
        await db.commit()
    logger.info(f"用户消息保存成功: session={session_uuid}, message_id={user_message_id}, messages_count={user_msg_count}")

    # LEARN W-C：解析本请求 A/B 变体 → 白名单参数覆盖（含 search_mode）
    ab_config = await _resolve_ab_config(current_user.user_id, str(session_uuid))
    effective_search_mode = (ab_config or {}).get("search_mode") or (request.search_mode or "simple")

    # 生成回答（传递历史消息和搜索参数）——此处不持有任何 DB 会话
    ab_token = set_ab_config(ab_config)
    ab_t0 = time.perf_counter()
    try:
        answer, sources, source_metadata, answer_type = await rag_chain.run(
            request.question, kb_ids, history,
            use_web_search=request.use_web_search,
            search_mode=effective_search_mode,
            user_id=current_user.user_id,
            session_id=str(session_uuid)
        )
    finally:
        reset_ab_config(ab_token)
    await _record_ab_metric(ab_config, (time.perf_counter() - ab_t0) * 1000)

    # 记录策略执行到学习引擎（LEARN W-B：短路路径按开关补采各策略结果）
    execution_id = None
    try:
        decision = rag_chain.get_last_decision()
        if decision:
            execution_id = await learning_engine.record_chat_execution(
                session_id=str(session_uuid),
                question=request.question,
                decision=decision,
                used_knowledge_base=answer_type in ("knowledge_base", "hybrid_search"),
                strategy_manager=rag_chain.strategy_manager,
                history=history,
                user_id=current_user.user_id,
            )
    except Exception as e:
        logger.error(f"记录策略执行失败: {e}", exc_info=True)

    # 保存助手消息（附带 execution_id 用于后续反馈关联）+ 生成会话标题（短会话 2）
    # 原子追加，理由同用户消息
    generated_title = None
    async with async_session() as db:
        result = await db.execute(select(SessionModel).filter(SessionModel.id == session_uuid))
        session = result.scalars().first()
        if session is None:
            # 会话在生成期间被删除：跳过持久化，回答仍返回给调用方
            logger.warning(f"会话在回答生成期间被删除，跳过助手消息落库: session={session_uuid}")
            assistant_msg_count = 0
        else:
            assistant_msg_count = await append_session_message(db, session_uuid, {
                "id": assistant_message_id,
                "role": "assistant",
                "content": answer,
                "sources": sources,
                "source_metadata": source_metadata,
                "timestamp": datetime.now().isoformat(),
                "execution_id": execution_id
            })

            # 若会话仍为默认标题，根据首条问题生成标题
            title_generator = await TitleGenerator.get_instance()
            if title_generator.is_default_title(session_title):
                try:
                    session.title = await title_generator.generate_title(request.question)
                    generated_title = session.title
                    logger.info(f"会话标题已生成(非流式): session={session_uuid}, title={session.title}")
                except Exception as title_err:
                    logger.warning(f"生成会话标题失败(非流式): {title_err}")

            await db.commit()
    logger.info(f"助手消息保存成功(非流式): session={session_uuid}, message_id={assistant_message_id}, messages_count={assistant_msg_count}")

    return {
        "session_id": str(session_uuid),
        "message_id": assistant_message_id,
        "answer": answer,
        "sources": sources,
        "source_metadata": source_metadata,
        "answer_type": answer_type,
        "title": generated_title
    }

@router.post("/messages/{message_id}/feedback")
async def submit_message_feedback(
    message_id: str,
    request: FeedbackRequest,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    提交消息评价反馈

    Args:
        message_id: 消息ID
        request: 反馈数据（评分、原因、会话ID）
        db: 数据库会话
        current_user: 当前认证用户

    Returns:
        dict: {"id": 评价ID, "message": "评价提交成功"}
    """
    # 兼容旧版字符串 like/dislike，同时支持 1-5 整数评分
    if isinstance(request.rating, int):
        rating = request.rating
    elif request.rating in ('positive', 'like'):
        rating = 5
    elif request.rating in ('negative', 'dislike'):
        rating = 1
    else:
        try:
            rating = int(request.rating)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="无效的评分值，评分范围为1-5或like/dislike")

    if rating not in [1, 2, 3, 4, 5]:
        raise HTTPException(status_code=400, detail="无效的评分值，评分范围为1-5")

    # 映射 rating 到 feedback_score (-1.0 ~ 1.0)
    feedback_score = (rating - 3) / 2.0

    # 先校验会话归属并解析 execution_id，再写入反馈。
    # 此前先 commit 反馈、后 require_owner，导致对他人 session_id 提交反馈时
    # 数据已落库、响应却是 403（部分写入）。现在把归属校验前置到写入之前。
    execution_id = None
    try:
        if request.session_id:
            result = await db.execute(
                select(SessionModel).filter(SessionModel.id == _parse_session_id(request.session_id))
            )
            session = result.scalar_one_or_none()
            if session:
                require_owner(session.user_id, current_user)
                for msg in (session.messages or []):
                    if msg.get("id") == message_id:
                        execution_id = msg.get("execution_id")
                        break
        else:
            # 未提供 session_id 时仅在当前用户会话中回溯 execution_id。
            # W6 #78：用 JSONB @> 在服务端定位包含该消息的会话（可命中 GIN 索引），
            # 取代拉取最近 200 个会话的全部 messages 再 Python 遍历——此前每条
            # 反馈都要全量加载大量消息 JSON，开销随会话长度线性放大。
            result = await db.execute(
                select(SessionModel)
                .filter(
                    SessionModel.user_id == current_user.user_id,
                    SessionModel.messages.contains([{"id": message_id}]),
                )
                .order_by(SessionModel.updated_at.desc())
                .limit(1)
            )
            session = result.scalars().first()
            if session:
                require_owner(session.user_id, current_user)
                for msg in (session.messages or []):
                    if msg.get("id") == message_id:
                        execution_id = msg.get("execution_id")
                        break
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"解析反馈关联会话失败: {e}", exc_info=True)

    feedback = Feedback(
        session_id=_parse_session_id(request.session_id),
        message_id=message_id,
        owner_id=current_user.user_id,
        rating=rating,
        reason=request.reason
    )
    db.add(feedback)
    await db.commit()
    await db.refresh(feedback)

    # 记录反馈到学习引擎（失败不影响反馈已提交的事实）
    try:
        if execution_id:
            await learning_engine.record_feedback(
                execution_id=execution_id,
                feedback_score=feedback_score,
                reason=request.reason,
                owner_id=current_user.user_id,
                enforce_owner=True,
            )
            logger.info(f"学习反馈已记录: execution_id={execution_id}, score={feedback_score}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"记录学习反馈失败: {e}", exc_info=True)

    return {"id": str(feedback.id), "message": "评价提交成功"}
