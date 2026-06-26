"""
会话管理API - Session API

提供会话的CRUD操作接口，包括：
1. 创建会话
2. 获取会话列表
3. 获取会话详情（含消息历史）
4. 更新会话标题
5. 删除会话
"""

import logging
import asyncio
import hashlib
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel
from typing import List, Optional
import uuid
from src.database import get_db
from src.models import Session as SessionModel, Feedback as FeedbackModel
from src.services.vector_store import VectorStoreManager
from src.services.rag_chain import RAGChain

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["sessions"])

# 快捷问题缓存：key 为历史问题哈希，value 为 (缓存时间, 问题列表)
_quick_questions_cache: dict[str, tuple[datetime, list[str]]] = {}
_QUICK_QUESTIONS_CACHE_TTL = timedelta(hours=24)

# 全局复用的 RAGChain 与 VectorStoreManager 实例，避免每次请求重复初始化
_rag_chain_instance: Optional[RAGChain] = None
_vector_store_instance: Optional[VectorStoreManager] = None


def _get_quick_questions_cache_key(questions: list[str]) -> str:
    """根据历史问题列表生成缓存键"""
    normalized = "\n".join(sorted(q.strip() for q in questions if q.strip()))
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()


def _get_cached_quick_questions(questions: list[str]) -> Optional[list[str]]:
    """获取缓存的快捷问题，若已过期则返回 None"""
    key = _get_quick_questions_cache_key(questions)
    cached = _quick_questions_cache.get(key)
    if cached is None:
        return None
    cached_at, suggestions = cached
    if datetime.now() - cached_at > _QUICK_QUESTIONS_CACHE_TTL:
        _quick_questions_cache.pop(key, None)
        return None
    return suggestions


def _set_cached_quick_questions(questions: list[str], suggestions: list[str]) -> None:
    """缓存生成的快捷问题"""
    key = _get_quick_questions_cache_key(questions)
    _quick_questions_cache[key] = (datetime.now(), suggestions)


async def _get_rag_chain() -> RAGChain:
    """获取全局复用的 RAGChain 实例（异步懒加载）"""
    global _rag_chain_instance, _vector_store_instance
    if _rag_chain_instance is None:
        _vector_store_instance = await VectorStoreManager.get_instance()
        _rag_chain_instance = await RAGChain.get_instance(_vector_store_instance)
        logger.info("RAGChain 全局实例初始化完成")
    return _rag_chain_instance


class SessionResponse(BaseModel):
    """会话响应数据模型"""
    id: str
    title: str
    user_id: str
    message_count: int
    last_message: Optional[str] = None
    created_at: str
    updated_at: str


class CreateSessionRequest(BaseModel):
    """创建会话请求数据模型"""
    title: Optional[str] = None
    kb_ids: Optional[List[str]] = None


class UpdateSessionRequest(BaseModel):
    """更新会话请求数据模型"""
    title: str
    kb_ids: Optional[List[str]] = None


class BatchDeleteRequest(BaseModel):
    """批量删除会话请求数据模型"""
    ids: List[str]


@router.post("/")
async def create_session(request: CreateSessionRequest = Body(default=None), db: AsyncSession = Depends(get_db)):
    """
    创建新会话
    
    Args:
        request: 创建会话请求（可选）
        db: 数据库会话
        
    Returns:
        dict: {"id": 会话ID, "title": 会话标题}
    """
    title = request.title if request else None
    kb_ids = request.kb_ids if request else None
    
    from datetime import datetime
    session = SessionModel(
        user_id="default",
        title=title or "新会话",
        messages=[],
        kb_ids=kb_ids or [],
        updated_at=datetime.now()
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return {"id": str(session.id), "title": session.title}


@router.get("/", response_model=List[SessionResponse])
async def list_sessions(db: AsyncSession = Depends(get_db)):
    """
    获取会话列表
    
    Args:
        db: 数据库会话
        
    Returns:
        list: 会话列表（按更新时间倒序）
    """
    result = await db.execute(select(SessionModel).order_by(SessionModel.updated_at.desc()))
    sessions = result.scalars().all()
    return [
        SessionResponse(
            id=str(s.id),
            title=s.title,
            user_id=s.user_id,
            message_count=len(s.messages),
            last_message=s.messages[-1].get("content", "") if s.messages else None,
            created_at=s.created_at.isoformat() if s.created_at else "",
            updated_at=s.updated_at.isoformat() if s.updated_at else ""
        ) for s in sessions
    ]


_DEFAULT_QUICK_QUESTIONS = [
    "什么是RAG?",
    "介绍一下项目功能",
    "如何上传文档?",
    "支持哪些文件格式?"
]


@router.get("/quick_questions")
async def get_quick_questions(db: AsyncSession = Depends(get_db)):
    """
    获取基于用户历史的快捷问题
    
    根据用户历史提问记录，动态生成快捷问题，帮助用户快速访问常见问题。
    结果会按历史问题内容缓存 24 小时，并复用全局 RAGChain 实例，避免重复初始化。
    
    Args:
        db: 数据库会话
        
    Returns:
        dict: {"quick_questions": 快捷问题列表}
    """
    result = await db.execute(select(SessionModel).order_by(SessionModel.updated_at.desc()).limit(10))
    sessions = result.scalars().all()
    
    # 提取所有历史问题
    history_questions = []
    for session in sessions:
        for msg in session.messages:
            if msg.get("role") == "user" and msg.get("content"):
                history_questions.append(msg["content"])
    
    # 如果没有历史问题，返回默认问题
    if not history_questions:
        return {"quick_questions": _DEFAULT_QUICK_QUESTIONS}
    
    # 尝试命中缓存
    cached = _get_cached_quick_questions(history_questions)
    if cached is not None:
        logger.info("命中快捷问题缓存")
        return {"quick_questions": cached}
    
    # 使用LLM生成相关的快捷问题
    try:
        rag_chain = await _get_rag_chain()

        # 构建历史问题上下文
        history_context = "\n".join([f"用户: {q}" for q in history_questions[:10]])

        suggestions = await rag_chain.generate_suggestions(None, history_context, None)
        _set_cached_quick_questions(history_questions, suggestions)
        return {"quick_questions": suggestions}
    except Exception as e:
        logger.error(f"生成快捷问题失败: {str(e)}", exc_info=True)
        # 返回默认问题
        return {"quick_questions": _DEFAULT_QUICK_QUESTIONS}


@router.get("/{session_id}")
async def get_session(session_id: str, db: AsyncSession = Depends(get_db)):
    """
    获取会话详情（含消息历史）
    
    Args:
        session_id: 会话ID
        db: 数据库会话
        
    Returns:
        dict: 会话详情，包含消息历史
    """
    try:
        result = await db.execute(select(SessionModel).filter(SessionModel.id == uuid.UUID(session_id)))
        session = result.scalar_one_or_none()
        if not session:
            raise HTTPException(status_code=404, detail="会话不存在")
        return {
            "id": str(session.id),
            "title": session.title,
            "messages": session.messages,
            "kb_ids": session.kb_ids,
            "created_at": session.created_at.isoformat() if session.created_at else "",
            "updated_at": session.updated_at.isoformat() if session.updated_at else ""
        }
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的会话ID")


@router.put("/{session_id}")
async def update_session(session_id: str, request: UpdateSessionRequest, db: AsyncSession = Depends(get_db)):
    """
    更新会话标题
    
    Args:
        session_id: 会话ID
        request: 更新会话请求
        db: 数据库会话
        
    Returns:
        dict: {"message": "更新成功"}
    """
    try:
        result = await db.execute(select(SessionModel).filter(SessionModel.id == uuid.UUID(session_id)))
        session = result.scalar_one_or_none()
        if not session:
            raise HTTPException(status_code=404, detail="会话不存在")
        
        session.title = request.title
        if request.kb_ids is not None:
            session.kb_ids = request.kb_ids
        await db.commit()
        return {"message": "更新成功"}
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的会话ID")


@router.delete("/{session_id}")
async def delete_session(session_id: str, db: AsyncSession = Depends(get_db)):
    """
    删除会话
    
    Args:
        session_id: 会话ID
        db: 数据库会话
        
    Returns:
        dict: {"message": "删除成功"}
    """
    try:
        result = await db.execute(select(SessionModel).filter(SessionModel.id == uuid.UUID(session_id)))
        session = result.scalar_one_or_none()
        if not session:
            raise HTTPException(status_code=404, detail="会话不存在")
        
        await db.delete(session)
        await db.commit()
        return {"message": "删除成功"}
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的会话ID")


@router.post("/batch-delete")
async def batch_delete_sessions(request: BatchDeleteRequest, db: AsyncSession = Depends(get_db)):
    """
    批量删除会话
    
    先删除关联的反馈记录，再删除会话记录，避免外键约束冲突。
    两步操作在同一个数据库事务中执行，保证原子性。
    
    Args:
        request: 批量删除请求（会话ID列表）
        db: 数据库会话
        
    Returns:
        dict: {"deleted_count": 实际删除的会话数量}
    """
    if not request.ids:
        return {"deleted_count": 0}
    
    try:
        session_ids = [uuid.UUID(session_id) for session_id in request.ids]
    except ValueError:
        raise HTTPException(status_code=400, detail="包含无效的会话ID")
    
    try:
        # 先删除关联的反馈记录
        await db.execute(delete(FeedbackModel).where(FeedbackModel.session_id.in_(session_ids)))
        # 再删除会话记录
        result = await db.execute(delete(SessionModel).where(SessionModel.id.in_(session_ids)))
        await db.commit()
        return {"deleted_count": result.rowcount}
    except IntegrityError as e:
        await db.rollback()
        logger.error(f"批量删除会话失败（完整性约束）: {e}")
        raise HTTPException(status_code=400, detail="删除失败：会话存在关联数据，无法删除")