"""聊天辅助路由：审批回执、推荐问题、上下文增强、改写、分类、对比。"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import asyncio
import uuid
import logging
from src.database import get_db
from src.auth import get_current_user, CurrentUser, require_owner
from src.models import Session as SessionModel, KnowledgeBase
from src.services.rag_chain import RAGChain
from src.services.vector_store import VectorStoreManager
from src.utils.validators import parse_uuid_list, validate_kb_ownership

from .common import (
    ApprovalResolveRequest,
    ClassifyRequest,
    CompareRequest,
    EnhanceContextRequest,
    RewriteRequest,
    SuggestionRequest,
    _parse_session_id,
)

logger = logging.getLogger("src.api.chat")

router = APIRouter(tags=["chat"])

@router.post("/approvals/{approval_id}")
async def resolve_approval(
    approval_id: str,
    payload: ApprovalResolveRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """回执一次高危工具审批；仅发起审批的用户可操作。

    返回 404（记录不存在/已过期）、403（非发起人）、409（已处理）。
    """
    from src.services.guardrails import get_approval_gate

    ok, code = await get_approval_gate().resolve(
        approval_id,
        approved=payload.approved,
        user_id=current_user.user_id,
        reason=payload.reason,
    )
    if ok:
        return {"status": "ok", "approved": payload.approved}
    if code == "not_found":
        raise HTTPException(status_code=404, detail="审批不存在或已过期")
    if code == "forbidden":
        raise HTTPException(status_code=403, detail="无权处理该审批")
    raise HTTPException(status_code=409, detail="审批已处理")

@router.post("/suggestions")
async def get_suggestions(
    request: SuggestionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    获取基于上下文的智能推荐问题

    根据当前问题和历史对话，生成相关的推荐问题，帮助用户探索更多相关内容

    Args:
        request: 请求数据（当前问题、会话ID、知识库ID列表）
        db: 数据库会话
        current_user: 当前认证用户

    Returns:
        dict: {"suggestions": 推荐问题列表}
    """
    # 知识库 ID 校验：格式 + 归属
    kb_ids = parse_uuid_list(request.kb_ids)
    await validate_kb_ownership(db, kb_ids, current_user)

    # 获取历史对话
    history = []
    if request.session_id:
        result = await db.execute(select(SessionModel).filter(SessionModel.id == _parse_session_id(request.session_id)))
        session = result.scalar_one_or_none()
        if session:
            require_owner(session.user_id, current_user)
            history = session.messages.copy()

    # 构建历史上下文
    recent_history = history[-5:] if len(history) > 5 else history
    history_context = "\n".join([f"{msg['role']}: {msg['content']}" for msg in recent_history])

    # 使用LLM生成推荐问题
    vector_store = await VectorStoreManager.get_instance()
    rag_chain = await RAGChain.get_instance(vector_store)

    try:
        # 辅助功能必须有硬上限：前端超时后旧请求仍会占用本地模型队列，
        # 8s 内未完成直接取消并返回空列表，避免推荐反压主回答。
        suggestions = await asyncio.wait_for(
            rag_chain.generate_suggestions(request.question, history_context, kb_ids),
            timeout=8.0,
        )
        return {"suggestions": suggestions}
    except asyncio.TimeoutError:
        logger.warning("生成推荐问题超时，跳过本次推荐")
        return {"suggestions": []}
    except Exception as e:
        logger.error(f"生成推荐问题失败: {str(e)}", exc_info=True)
        return {"suggestions": []}

@router.post("/enhance_context")
async def enhance_context(
    request: EnhanceContextRequest,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    增强上下文处理，包括指代消解和上下文优化

    Args:
        request: 请求数据（问题、会话ID）
        db: 数据库会话
        current_user: 当前认证用户

    Returns:
        dict: 包含增强后的问题和上下文信息
    """
    history = []
    if request.session_id:
        result = await db.execute(select(SessionModel).filter(SessionModel.id == _parse_session_id(request.session_id)))
        session = result.scalar_one_or_none()
        if session:
            require_owner(session.user_id, current_user)
            history = session.messages.copy()

    vector_store = await VectorStoreManager.get_instance()
    rag_chain = await RAGChain.get_instance(vector_store)

    try:
        result = await rag_chain.enhance_context(request.question, history, request.session_id)
        return result
    except Exception as e:
        logger.error(f"上下文增强失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="上下文增强失败，请稍后重试")

@router.post("/rewrite")
async def rewrite_question(request: RewriteRequest):
    """
    重写问题，使其更加清晰、完整

    Args:
        request: 请求数据（问题内容）

    Returns:
        dict: {"rewritten": 重写后的问题, "original": 原始问题, "changes": 改动说明}
    """
    vector_store = await VectorStoreManager.get_instance()
    rag_chain = await RAGChain.get_instance(vector_store)

    try:
        result = await rag_chain.rewrite_question(request.question)
        return result
    except Exception as e:
        logger.error(f"问题重写失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="问题重写失败，请稍后重试")

@router.post("/classify")
async def classify_question(request: ClassifyRequest):
    """
    识别问题类型

    Args:
        request: 请求数据（问题内容）

    Returns:
        dict: {"type": 问题类型, "subtype": 子类型, "confidence": 置信度, "description": 类型说明}
    """
    vector_store = await VectorStoreManager.get_instance()
    rag_chain = await RAGChain.get_instance(vector_store)

    try:
        result = await rag_chain.classify_question(request.question)
        return result
    except Exception as e:
        logger.error(f"问题分类失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="问题分类失败，请稍后重试")

@router.post("/compare")
async def compare_knowledge_bases(
    request: CompareRequest,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    对比多个知识库的答案差异

    Args:
        request: 请求数据（问题和知识库ID列表）
        current_user: 当前认证用户

    Returns:
        dict: 各知识库的答案对比结果
    """
    if not request.kb_ids or len(request.kb_ids) < 2:
        raise HTTPException(status_code=400, detail="至少需要选择2个知识库进行对比")

    # 知识库 ID 校验：格式 + 归属
    kb_ids = parse_uuid_list(request.kb_ids)
    if len(kb_ids) < 2:
        raise HTTPException(status_code=400, detail="至少需要选择2个不同的知识库进行对比")
    await validate_kb_ownership(db, kb_ids, current_user)

    vector_store = await VectorStoreManager.get_instance()
    rag_chain = await RAGChain.get_instance(vector_store)

    try:
        # 查询知识库名称映射，避免在同步上下文中访问数据库
        kb_name_map = {}
        for kb_id in kb_ids:
            result = await db.execute(select(KnowledgeBase).filter(KnowledgeBase.id == uuid.UUID(kb_id)))
            kb = result.scalar_one_or_none()
            kb_name_map[kb_id] = kb.name if kb else kb_id[:8]

        result = await rag_chain.compare_knowledge_bases(
            request.question, kb_ids, kb_name_map
        )
        return {"comparison": result, "question": request.question}
    except Exception as e:
        logger.error(f"答案对比失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="答案对比失败，请稍后重试")
