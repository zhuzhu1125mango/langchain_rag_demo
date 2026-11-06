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
from collections import OrderedDict
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Body, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import Text, cast, delete, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel
from typing import List, Optional
import uuid
from src.config import settings
from src.database import get_db
from src.auth import get_current_user, CurrentUser, require_owner
from src.models import Session as SessionModel, Feedback as FeedbackModel
from src.services.rag_chain import RAGChain
from src.services.session_export import build_export_filename, render_session_markdown

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["sessions"])

# SEC-18：会话列表单页上限（默认 100）；批量删除上限见 settings.security.BATCH_DELETE_MAX_IDS
SESSION_LIST_MAX_LIMIT = 500

# UX-5：搜索关键词长度上限。LIKE 模式越长越慢，用户也不会输入 200 字去搜历史。
SESSION_SEARCH_MAX_KEYWORD = 100


def _escape_like(value: str) -> str:
    """转义 LIKE 通配符。

    用户搜「100%」时，`%` 不该被当成通配符——否则会匹配到所有以 100 开头的内容。
    反斜杠必须**最先**替换，否则会把后面新加进去的反斜杠再转义一遍。
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _build_session_filters(
    user_id: str,
    keyword: Optional[str],
    kb_id: Optional[str],
    date_from: Optional[date],
    date_to: Optional[date],
) -> list:
    """组装会话列表的 WHERE 条件（全部以 AND 组合）。

    拆成独立函数是为了让「筛选口径」只有一处：以后加导出、加统计都复用同一份，
    否则容易出现「列表按标题搜、导出按标题+正文搜」这种对不上的情况。
    """
    conditions = [SessionModel.user_id == user_id]

    if keyword:
        kw = keyword.strip()[:SESSION_SEARCH_MAX_KEYWORD]
        if kw:
            pattern = f"%{_escape_like(kw)}%"
            # 标题或消息正文命中即可。messages 是 JSONB，转 text 后整段匹配：
            # 没有索引，但会话表在万级量下可接受，换来的是「能搜到聊天内容」这个能力。
            conditions.append(
                or_(
                    SessionModel.title.ilike(pattern, escape="\\"),
                    cast(SessionModel.messages, Text).ilike(pattern, escape="\\"),
                )
            )

    if kb_id:
        # kb_ids 列是 JSON（不是 JSONB），必须转成 JSONB 才能用包含运算符 @>
        conditions.append(cast(SessionModel.kb_ids, JSONB).contains([kb_id]))

    # 日期按「最近活动」筛（updated_at），与列表排序口径一致
    if date_from:
        conditions.append(
            SessionModel.updated_at >= datetime.combine(date_from, time.min, tzinfo=timezone.utc)
        )
    if date_to:
        # 含当天：用「次日零点」做开区间上界，避免 23:59:59 这类边界把当天数据漏掉
        conditions.append(
            SessionModel.updated_at
            < datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=timezone.utc)
        )

    return conditions

# 快捷问题缓存：key 为历史问题哈希，value 为 (缓存时间, 问题列表)
# OrderedDict 实现 LRU：命中移到末尾，超限时从头部（最久未使用）淘汰
_quick_questions_cache: OrderedDict[str, tuple[datetime, list[str]]] = OrderedDict()
_QUICK_QUESTIONS_CACHE_TTL = timedelta(hours=24)
_QUICK_QUESTIONS_CACHE_MAX_SIZE = 256

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
    # 命中时刷新 LRU 顺序
    _quick_questions_cache.move_to_end(key)
    return suggestions


def _set_cached_quick_questions(questions: list[str], suggestions: list[str]) -> None:
    """缓存生成的快捷问题（写时惰性清理过期项 + LRU 容量上限）"""
    key = _get_quick_questions_cache_key(questions)
    now = datetime.now()
    # 写时惰性清理过期项，避免过期堆积挤占容量
    expired_keys = [
        k for k, (cached_at, _) in _quick_questions_cache.items()
        if now - cached_at > _QUICK_QUESTIONS_CACHE_TTL
    ]
    for k in expired_keys:
        _quick_questions_cache.pop(k, None)
    _quick_questions_cache[key] = (now, suggestions)
    _quick_questions_cache.move_to_end(key)
    # 超出容量上限时淘汰最久未使用的条目
    while len(_quick_questions_cache) > _QUICK_QUESTIONS_CACHE_MAX_SIZE:
        _quick_questions_cache.popitem(last=False)


async def _get_rag_chain() -> RAGChain:
    """获取全局复用的 RAGChain 单例实例。

    RAGChain 已在应用启动预热阶段完成初始化，这里直接获取，避免重复创建。
    若因测试等场景未预热，则降级为懒加载初始化。
    """
    return await RAGChain.get_instance()


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
async def create_session(
    request: CreateSessionRequest = Body(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    创建新会话

    Args:
        request: 创建会话请求（可选）
        db: 数据库会话
        current_user: 当前认证用户

    Returns:
        dict: {"id": 会话ID, "title": 会话标题}
    """
    title = request.title if request else None
    kb_ids = request.kb_ids if request else None

    from datetime import datetime, timezone
    session = SessionModel(
        user_id=current_user.user_id,
        title=title or "新会话",
        messages=[],
        kb_ids=kb_ids or [],
        updated_at=datetime.now(timezone.utc)
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return {
        "id": str(session.id),
        "title": session.title,
        "kb_ids": session.kb_ids,
    }


@router.get("/", response_model=List[SessionResponse])
async def list_sessions(
    skip: int = Query(0, ge=0, description="跳过数量（分页参数）"),
    limit: int = Query(100, ge=1, le=SESSION_LIST_MAX_LIMIT, description="每页数量（分页参数）"),
    q: Optional[str] = Query(
        None,
        max_length=SESSION_SEARCH_MAX_KEYWORD,
        description="关键词（匹配标题与消息正文，不区分大小写）",
    ),
    kb_id: Optional[str] = Query(None, description="按关联知识库筛选"),
    date_from: Optional[date] = Query(None, description="按最近活动日期筛选（含当天的开始）"),
    date_to: Optional[date] = Query(None, description="按最近活动日期筛选（含当天的结束）"),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    获取当前用户的会话列表（SEC-18：分页返回，避免大会话历史一次性拉全）

    UX-5 起支持筛选：`q` 关键词、`kb_id` 知识库、`date_from`/`date_to` 日期范围。
    四个参数都可省略，不传时行为与之前完全一致（只按更新时间倒序分页）。

    Args:
        skip: 跳过数量
        limit: 每页数量（默认 100，上限见 SESSION_LIST_MAX_LIMIT）
        q: 关键词，匹配标题或任一消息内容
        kb_id: 只返回关联了该知识库的会话
        date_from: 最近活动时间下界（含）
        date_to: 最近活动时间上界（含当天）
        db: 数据库会话
        current_user: 当前认证用户

    Returns:
        list: 会话列表（按更新时间倒序）
    """
    conditions = _build_session_filters(current_user.user_id, q, kb_id, date_from, date_to)
    result = await db.execute(
        select(SessionModel)
        .filter(*conditions)
        .order_by(SessionModel.updated_at.desc())
        .offset(skip)
        .limit(limit)
    )
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
async def get_quick_questions(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    获取基于当前用户历史的快捷问题

    根据用户历史提问记录，动态生成快捷问题，帮助用户快速访问常见问题。
    结果会按历史问题内容缓存 24 小时，并复用全局 RAGChain 实例，避免重复初始化。

    Args:
        db: 数据库会话
        current_user: 当前认证用户

    Returns:
        dict: {"quick_questions": 快捷问题列表}
    """
    result = await db.execute(
        select(SessionModel)
        .filter(SessionModel.user_id == current_user.user_id)
        .order_by(SessionModel.updated_at.desc())
        .limit(10)
    )
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
async def get_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    获取会话详情（含消息历史）

    Args:
        session_id: 会话ID
        db: 数据库会话
        current_user: 当前认证用户

    Returns:
        dict: 会话详情，包含消息历史
    """
    try:
        result = await db.execute(select(SessionModel).filter(SessionModel.id == uuid.UUID(session_id)))
        session = result.scalar_one_or_none()
        if not session:
            raise HTTPException(status_code=404, detail="会话不存在")
        require_owner(session.user_id, current_user)
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
async def update_session(
    session_id: str,
    request: UpdateSessionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    更新会话标题

    Args:
        session_id: 会话ID
        request: 更新会话请求
        db: 数据库会话
        current_user: 当前认证用户

    Returns:
        dict: 更新后的会话信息
    """
    try:
        result = await db.execute(select(SessionModel).filter(SessionModel.id == uuid.UUID(session_id)))
        session = result.scalar_one_or_none()
        if not session:
            raise HTTPException(status_code=404, detail="会话不存在")
        require_owner(session.user_id, current_user)

        session.title = request.title
        if request.kb_ids is not None:
            session.kb_ids = request.kb_ids
        await db.commit()
        await db.refresh(session)
        return {
            "id": str(session.id),
            "title": session.title,
            "kb_ids": session.kb_ids,
            "message": "更新成功",
        }
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的会话ID")


@router.delete("/{session_id}")
async def delete_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    删除会话

    Args:
        session_id: 会话ID
        db: 数据库会话
        current_user: 当前认证用户

    Returns:
        dict: {"message": "删除成功"}
    """
    try:
        result = await db.execute(select(SessionModel).filter(SessionModel.id == uuid.UUID(session_id)))
        session = result.scalar_one_or_none()
        if not session:
            raise HTTPException(status_code=404, detail="会话不存在")
        require_owner(session.user_id, current_user)

        owner_id = str(session.user_id)
        await db.delete(session)
        await db.commit()

        # 记忆向量清理：会话行删掉后，uuid5(agent-memory:{session_id}) 这批切片
        # 再无任何元数据指向，只能靠这里清理。失败不阻断删除（用户点删除必须生效），
        # 但按 REPORTABLE 记录——留下的对话摘要属于隐私数据。
        from src.services.agent_memory import delete_session_memories

        await delete_session_memories(
            [session_id], action="session.delete.memory_vectors", user_id=owner_id
        )

        return {"message": "删除成功"}
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的会话ID")


@router.get("/{session_id}/export")
async def export_session(
    session_id: str,
    format: str = Query("markdown", pattern="^(markdown|md|json)$", description="导出格式：markdown / json"),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    导出单条会话（UX-5）

    Markdown 用于「贴到别处」；JSON 用于程序处理（含完整消息字段）。
    两者都只导出当前用户自己的会话。

    Args:
        session_id: 会话ID
        format: markdown（默认）/ json
        db: 数据库会话
        current_user: 当前认证用户

    Returns:
        Markdown 文本（带 Content-Disposition 下载头）或 JSON
    """
    try:
        result = await db.execute(select(SessionModel).filter(SessionModel.id == uuid.UUID(session_id)))
        session = result.scalar_one_or_none()
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的会话ID")

    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    require_owner(session.user_id, current_user)

    meta = {
        "id": str(session.id),
        "title": session.title,
        "created_at": session.created_at.isoformat() if session.created_at else "",
        "updated_at": session.updated_at.isoformat() if session.updated_at else "",
    }
    messages = session.messages or []

    if format == "json":
        return JSONResponse({"session": meta, "messages": messages})

    markdown = render_session_markdown(meta, messages)
    filename = build_export_filename(session.title, session.id)
    # 中文文件名必须走 RFC 5987 的 filename*，否则浏览器拿到的是乱码或被截断；
    # 同时保留一个纯 ASCII 的 filename 作为老客户端回退。
    disposition = (
        f'attachment; filename="session-{str(session.id)[:8]}.md"; '
        f"filename*=UTF-8''{quote(filename)}"
    )
    return PlainTextResponse(
        markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": disposition},
    )


@router.post("/batch-delete")
async def batch_delete_sessions(
    request: BatchDeleteRequest,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    批量删除会话

    仅删除当前用户拥有的会话。先删除关联的反馈记录，再删除会话记录，避免外键约束冲突。
    两步操作在同一个数据库事务中执行，保证原子性。

    Args:
        request: 批量删除请求（会话ID列表）
        db: 数据库会话
        current_user: 当前认证用户

    Returns:
        dict: {"deleted_count": 实际删除的会话数量}
    """
    if not request.ids:
        return {"deleted_count": 0}

    max_ids = int(settings.security.BATCH_DELETE_MAX_IDS)
    if max_ids and len(request.ids) > max_ids:
        raise HTTPException(
            status_code=400,
            detail=f"单次批量删除最多允许 {max_ids} 个会话，当前 {len(request.ids)} 个",
        )

    try:
        session_ids = [uuid.UUID(session_id) for session_id in request.ids]
    except ValueError:
        raise HTTPException(status_code=400, detail="包含无效的会话ID")

    try:
        # 先确认「确实属于当前用户、且即将被删」的会话：批量 delete 只给出总 rowcount，
        # 无法按 id 区分；若照单全清记忆向量，会把别人会话的记忆一并删掉。
        owned_ids = (
            await db.execute(
                select(SessionModel.id).where(
                    SessionModel.id.in_(session_ids),
                    SessionModel.user_id == current_user.user_id,
                )
            )
        ).scalars().all()

        # 先删除关联的反馈记录（仅当前用户的）
        await db.execute(
            delete(FeedbackModel).where(
                FeedbackModel.session_id.in_(session_ids),
                FeedbackModel.owner_id == current_user.user_id,
            )
        )
        # 再删除当前用户的会话记录
        result = await db.execute(
            delete(SessionModel).where(
                SessionModel.id.in_(session_ids),
                SessionModel.user_id == current_user.user_id,
            )
        )
        await db.commit()

        # 记忆向量与 DB 行同步清理（失败仅记 REPORTABLE，不阻断已提交的删除）
        from src.services.agent_memory import delete_session_memories

        await delete_session_memories(
            [str(i) for i in owned_ids],
            action="session.batch_delete.memory_vectors",
            user_id=str(current_user.user_id),
        )

        return {"deleted_count": result.rowcount}
    except IntegrityError as e:
        await db.rollback()
        logger.error(f"批量删除会话失败（完整性约束）: {e}")
        raise HTTPException(status_code=400, detail="删除失败：会话存在关联数据，无法删除")