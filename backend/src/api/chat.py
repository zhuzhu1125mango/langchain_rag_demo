"""
聊天问答API - Chat API

提供聊天问答相关接口，包括：
1. 发送消息（非流式）
2. 流式回答（SSE）
3. 支持指定知识库定向提问
4. 敏感词过滤
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, Field
from typing import List, Optional, Union, Annotated
import asyncio
import uuid
import json
import logging
from datetime import datetime
from src.database import get_db, async_session
from src.auth import get_current_user, CurrentUser, require_owner
from src.models import Session as SessionModel, Feedback, KnowledgeBase
from src.services.rag_chain import RAGChain
from src.services.vector_store import VectorStoreManager
from src.services.session_service import append_session_message
from src.services.learning_engine import learning_engine
from src.services.title_generator import TitleGenerator
from src.utils.sensitive_words import sensitive_filter
from src.utils.validators import parse_uuid_list, validate_kb_ownership

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


class MessageRequest(BaseModel):
    """消息请求数据模型"""
    question: str
    session_id: Optional[str] = None
    kb_ids: Optional[List[str]] = None
    use_web_search: Optional[bool] = False
    search_mode: Optional[str] = "simple"


@router.post("/messages")
async def send_message(
    request: MessageRequest,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    发送消息并获取回答（非流式）

    Args:
        request: 请求数据（问题、会话ID、知识库ID列表）
        db: 数据库会话
        current_user: 当前认证用户

    Returns:
        dict: {"session_id": 会话ID, "answer": 回答内容, "sources": 来源列表, "answer_type": 回答类型}
    """
    # 敏感词检测
    detected, word, category = sensitive_filter.detect(request.question)
    if detected:
        raise HTTPException(status_code=400, detail=f"输入包含敏感内容 [{category}]: {word}")

    # 知识库 ID 校验：格式 + 归属
    kb_ids = parse_uuid_list(request.kb_ids)
    await validate_kb_ownership(db, kb_ids, current_user)

    # 加载向量库和RAG链
    vector_store = await VectorStoreManager.get_instance()
    rag_chain = await RAGChain.get_instance(vector_store)

    # 获取或创建会话
    if request.session_id:
        result = await db.execute(select(SessionModel).filter(SessionModel.id == uuid.UUID(request.session_id)))
        session = result.scalar_one_or_none()
        if not session:
            raise HTTPException(status_code=404, detail="会话不存在")
        require_owner(session.user_id, current_user)
        history = session.messages.copy()
    else:
        session = SessionModel(
            user_id=current_user.user_id,
            title=request.question[:50],
            messages=[],
            kb_ids=kb_ids,
            updated_at=datetime.now()
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)
        history = []

    # 生成消息ID
    user_message_id = str(uuid.uuid4())
    assistant_message_id = str(uuid.uuid4())

    # 保存用户消息（在生成回答之前保存，以便后续历史记录完整）
    # 原子追加：并发写同一会话时由 PG 行锁串行化，不丢消息
    user_msg_count = await append_session_message(db, session.id, {
        "id": user_message_id,
        "role": "user",
        "content": request.question,
        "timestamp": datetime.now().isoformat()
    })
    await db.commit()
    logger.info(f"用户消息保存成功: session={session.id}, message_id={user_message_id}, messages_count={user_msg_count}")

    # 生成回答（传递历史消息和搜索参数）
    answer, sources, source_metadata, answer_type = await rag_chain.run(
        request.question, kb_ids, history,
        use_web_search=request.use_web_search,
        search_mode=request.search_mode or "simple"
    )

    # 记录策略执行到学习引擎
    execution_id = None
    try:
        decision = rag_chain.get_last_decision()
        if decision:
            execution_id = await learning_engine.record_execution(
                session_id=str(session.id),
                question=request.question,
                final_decision=decision.should_use_kb,
                final_confidence=decision.confidence,
                strategy_results=decision.strategy_results or {},
                used_knowledge_base=answer_type in ("knowledge_base", "hybrid_search")
            )
    except Exception as e:
        logger.error(f"记录策略执行失败: {e}", exc_info=True)

    # 保存助手消息（附带 execution_id 用于后续反馈关联）；原子追加，理由同用户消息
    assistant_msg_count = await append_session_message(db, session.id, {
        "id": assistant_message_id,
        "role": "assistant",
        "content": answer,
        "sources": sources,
        "source_metadata": source_metadata,
        "timestamp": datetime.now().isoformat(),
        "execution_id": execution_id
    })

    # 若会话仍为默认标题，根据首条问题生成标题
    generated_title = None
    title_generator = await TitleGenerator.get_instance()
    if title_generator.is_default_title(session.title):
        try:
            session.title = await title_generator.generate_title(request.question)
            generated_title = session.title
            logger.info(f"会话标题已生成(非流式): session={session.id}, title={session.title}")
        except Exception as title_err:
            logger.warning(f"生成会话标题失败(非流式): {title_err}")

    await db.commit()
    logger.info(f"助手消息保存成功(非流式): session={session.id}, message_id={assistant_message_id}, messages_count={assistant_msg_count}")

    return {
        "session_id": str(session.id),
        "message_id": assistant_message_id,
        "answer": answer,
        "sources": sources,
        "source_metadata": source_metadata,
        "answer_type": answer_type,
        "title": generated_title
    }


@router.get("/stream")
async def stream_answer(
    question: str = Query(...),
    session_id: Optional[str] = Query(None),
    kb_ids: Optional[List[str]] = Query(None),
    use_web_search: Optional[bool] = Query(False),
    search_mode: Optional[str] = Query("simple"),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    流式回答接口（SSE）

    Args:
        question: 用户问题
        session_id: 会话ID（可选）
        kb_ids: 知识库ID列表（可选）
        current_user: 当前认证用户

    Returns:
        StreamingResponse: 流式响应（SSE格式）
    """
    # 敏感词检测
    detected, word, category = sensitive_filter.detect(question)
    if detected:
        async def error_generator():
            yield json.dumps({"type": "error", "error": f"输入包含敏感内容 [{category}]: {word}"})
        return StreamingResponse(error_generator(), media_type="text/event-stream")

    # 知识库 ID 校验：格式 + 归属（在流式响应开始前完成，错误以 4xx 返回）
    validated_kb_ids = parse_uuid_list(kb_ids)
    async with async_session() as _vdb:
        await validate_kb_ownership(_vdb, validated_kb_ids, current_user)
    kb_ids = validated_kb_ids

    # 加载向量库和RAG链
    vector_store = await VectorStoreManager.get_instance()
    rag_chain = await RAGChain.get_instance(vector_store)

    # 获取或创建会话（在独立session中完成，避免StreamingResponse生命周期问题）
    current_session_id = None
    history = []
    user_message_id = str(uuid.uuid4())
    assistant_message_id = str(uuid.uuid4())

    async with async_session() as db:
        if session_id:
            result = await db.execute(select(SessionModel).filter(SessionModel.id == uuid.UUID(session_id)))
            session = result.scalar_one_or_none()
            if not session:
                async def error_generator():
                    yield json.dumps({"type": "error", "error": "会话不存在"})
                return StreamingResponse(error_generator(), media_type="text/event-stream")
            require_owner(session.user_id, current_user)
            current_session_id = str(session.id)
            history = session.messages.copy()
        else:
            session = SessionModel(
                user_id=current_user.user_id,
                title=question[:50],
                messages=[],
                kb_ids=kb_ids or [],
                updated_at=datetime.now()
            )
            db.add(session)
            await db.commit()
            await db.refresh(session)
            current_session_id = str(session.id)

        # 保存用户消息（原子追加：并发写同一会话不丢消息）
        await append_session_message(db, session.id, {
            "id": user_message_id,
            "role": "user",
            "content": question,
            "timestamp": datetime.now().isoformat()
        })
        await db.commit()
        logger.info(f"用户消息保存成功(流式): session={current_session_id}, message_id={user_message_id}")

    # 保存助手消息（流式生成结束或异常时调用）
    async def save_assistant_message(full_answer, sources, source_metadata, reasoning=None):
        generated_title = None
        async with async_session() as db:
            result = await db.execute(select(SessionModel).filter(SessionModel.id == uuid.UUID(current_session_id)))
            session = result.scalar_one_or_none()
            if not session:
                logger.warning(f"保存助手消息时未找到会话: {current_session_id}")
                return generated_title

            # 记录策略执行到学习引擎（失败不影响消息保存）
            execution_id = None
            try:
                decision = rag_chain.get_last_decision()
                if decision:
                    execution_id = await learning_engine.record_execution(
                        session_id=current_session_id,
                        question=question,
                        final_decision=decision.should_use_kb,
                        final_confidence=decision.confidence,
                        strategy_results=decision.strategy_results or {},
                        used_knowledge_base=len(source_metadata) > 0
                    )
            except Exception as exec_err:
                logger.error(f"记录策略执行失败: {exec_err}", exc_info=True)

            message_payload = {
                "id": assistant_message_id,
                "role": "assistant",
                "content": full_answer,
                "sources": sources,
                "source_metadata": source_metadata,
                "timestamp": datetime.now().isoformat(),
                "execution_id": execution_id,
            }
            if reasoning:
                message_payload["reasoning"] = reasoning
            # 原子追加（服务端 jsonb 拼接）；此处的 SELECT 仍保留用于读取 title 判断
            assistant_msg_count = await append_session_message(db, session.id, message_payload)

            # 若会话仍为默认标题，根据首条问题生成标题
            title_generator = await TitleGenerator.get_instance()
            if title_generator.is_default_title(session.title):
                try:
                    session.title = await title_generator.generate_title(question)
                    generated_title = session.title
                    logger.info(f"会话标题已生成(流式): session={current_session_id}, title={session.title}")
                except Exception as title_err:
                    logger.warning(f"生成会话标题失败(流式): {title_err}")

            await db.commit()
            logger.info(f"助手消息保存成功: session={current_session_id}, message_id={assistant_message_id}, content_length={len(full_answer)}, messages_count={assistant_msg_count}")
        return generated_title

    # 流式生成回答
    async def generate():
        full_answer = ""
        sources = []
        source_metadata = []
        answer_type = "llm_direct"
        reasoning_steps = []

        try:
            async for result in rag_chain.arun_stream(
                question, kb_ids, history,
                use_web_search=use_web_search,
                search_mode=search_mode or "simple"
            ):
                chunk, source_texts, source_meta, at = result

                # reasoning 事件：搜索/思考过程结构化数据
                if at == "reasoning":
                    try:
                        reasoning_data = json.loads(chunk) if chunk else {}
                    except Exception:
                        reasoning_data = {}
                    if reasoning_data and reasoning_data.get("type") == "reasoning":
                        reasoning_steps.append(reasoning_data)
                        logger.debug(f"SSE reasoning payload: {reasoning_data}")
                        yield f"data: {json.dumps(reasoning_data)}\n\n"
                    continue

                # 搜索状态事件（向后兼容）：映射为 reasoning 步骤
                if at == "search_status":
                    try:
                        status_data = json.loads(chunk) if chunk else {}
                    except Exception:
                        status_data = {}
                    reasoning_payload = {
                        "type": "reasoning",
                        "step": "web_search",
                        "status": status_data.get("status", "running"),
                        "title": "联网搜索",
                        "content": status_data.get("message", ""),
                        "metadata": {"sources_count": status_data.get("sources", 0)},
                    }
                    reasoning_steps.append(reasoning_payload)
                    logger.debug(f"SSE search_status mapped to reasoning: {reasoning_payload}")
                    yield f"data: {json.dumps(reasoning_payload)}\n\n"
                    continue

                # 过滤模型思考阶段产生的空内容，避免前端显示异常
                if chunk:
                    full_answer += chunk
                    content_payload = {'type': 'content', 'content': chunk}
                    logger.debug(f"SSE content payload: {content_payload}")
                    yield f"data: {json.dumps(content_payload)}\n\n"
                sources = source_texts
                source_metadata = source_meta
                answer_type = at

        except Exception as e:
            logger.error(f"流式生成异常: {str(e)}", exc_info=True)
            # 如果已生成部分内容，尝试保存，避免用户消息孤立
            if full_answer:
                try:
                    save_task = asyncio.create_task(save_assistant_message(full_answer, sources, source_metadata, reasoning_steps))
                    await asyncio.shield(save_task)
                except Exception as save_err:
                    logger.error(f"异常时保存部分助手消息失败: {save_err}", exc_info=True)
            error_payload = {'type': 'error', 'error': f'生成回答失败: {str(e)}'}
            logger.debug(f"SSE error payload: {error_payload}")
            yield f"data: {json.dumps(error_payload)}\n\n"
            return

        # 构建来源信息（包含完整的元数据，支持答案溯源与网页来源展示）
        source_info = []
        for i, meta in enumerate(source_metadata, 1):
            info = {
                "index": i,
                "document_id": meta.get('document_id', ''),
                "filename": meta.get('filename', 'unknown'),
                "title": meta.get('title') or meta.get('filename', 'unknown'),
                "chunk_index": meta.get('chunk_index', 0),
                "total_chunks": meta.get('total_chunks', 1),
                "content": meta.get('page_content', ''),
                "source": meta.get('source', ''),
                "url": meta.get('url', ''),
            }
            # 网页来源使用标题作为展示名
            if meta.get('source') == 'web_search' or meta.get('url'):
                info["source_type"] = "web"
                if not info["filename"] or info["filename"] == 'web_search':
                    info["filename"] = meta.get('title', '网页来源')
            else:
                info["source_type"] = "kb"
            source_info.append(info)

        # 在发送 end 事件前先保存助手消息，确保客户端断开前消息已持久化
        generated_title = None
        try:
            save_task = asyncio.create_task(save_assistant_message(full_answer, sources, source_metadata, reasoning_steps))
            generated_title = await asyncio.shield(save_task)
        except Exception as e:
            logger.error(f"保存助手消息失败: {str(e)}", exc_info=True)
            error_payload = {'type': 'error', 'error': f'保存消息失败: {str(e)}'}
            logger.debug(f"SSE error payload: {error_payload}")
            yield f"data: {json.dumps(error_payload)}\n\n"
            return

        end_payload = {
            'type': 'end',
            'message_id': assistant_message_id,
            'session_id': current_session_id,
            'sources': source_info,
            'answer_type': answer_type,
            'reasoning': reasoning_steps,
        }
        if generated_title:
            end_payload['title'] = generated_title
        logger.info(f"SSE end payload: {end_payload}")
        yield f"data: {json.dumps(end_payload)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


class SuggestionRequest(BaseModel):
    """推荐问题请求数据模型"""
    question: Optional[str] = None
    session_id: Optional[str] = None
    kb_ids: Optional[List[str]] = None


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
        result = await db.execute(select(SessionModel).filter(SessionModel.id == uuid.UUID(request.session_id)))
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
        suggestions = await rag_chain.generate_suggestions(
            request.question, history_context, kb_ids
        )
        return {"suggestions": suggestions}
    except Exception as e:
        logger.error(f"生成推荐问题失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"生成推荐问题失败: {str(e)}")


class FeedbackRequest(BaseModel):
    """消息反馈请求数据模型"""
    rating: Annotated[Union[int, str], Field(union_mode='left_to_right')]
    reason: str = ""
    session_id: Optional[str] = None


class RewriteRequest(BaseModel):
    """问题重写请求数据模型"""
    question: str


class ClassifyRequest(BaseModel):
    """问题分类请求数据模型"""
    question: str


class CompareRequest(BaseModel):
    """答案对比请求数据模型"""
    question: str
    kb_ids: List[str]


class EnhanceContextRequest(BaseModel):
    """上下文增强请求数据模型"""
    question: str
    session_id: Optional[str] = None


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
        result = await db.execute(select(SessionModel).filter(SessionModel.id == uuid.UUID(request.session_id)))
        session = result.scalar_one_or_none()
        if session:
            require_owner(session.user_id, current_user)
            history = session.messages.copy()

    vector_store = await VectorStoreManager.get_instance()
    rag_chain = await RAGChain.get_instance(vector_store)

    try:
        result = await rag_chain.enhance_context(request.question, history)
        return result
    except Exception as e:
        logger.error(f"上下文增强失败: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"上下文增强失败: {str(e)}")


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

    feedback = Feedback(
        session_id=uuid.UUID(request.session_id) if request.session_id else None,
        message_id=message_id,
        owner_id=current_user.user_id,
        rating=rating,
        reason=request.reason
    )
    db.add(feedback)
    await db.commit()
    await db.refresh(feedback)

    # 查找关联的 execution_id 并记录反馈到学习引擎
    try:
        execution_id = None
        if request.session_id:
            result = await db.execute(
                select(SessionModel).filter(SessionModel.id == uuid.UUID(request.session_id))
            )
            session = result.scalar_one_or_none()
            if session and session.messages:
                for msg in session.messages:
                    if msg.get("id") == message_id:
                        execution_id = msg.get("execution_id")
                        break
        else:
            # 未提供 session_id 时遍历查询（兼容性处理）
            result = await db.execute(select(SessionModel))
            sessions = result.scalars().all()
            for session in sessions:
                if session.messages:
                    for msg in session.messages:
                        if msg.get("id") == message_id:
                            execution_id = msg.get("execution_id")
                            break
                if execution_id:
                    break

        if execution_id:
            await learning_engine.record_feedback(
                execution_id=execution_id,
                feedback_score=feedback_score,
                reason=request.reason
            )
            logger.info(f"学习反馈已记录: execution_id={execution_id}, score={feedback_score}")
    except Exception as e:
        logger.error(f"记录学习反馈失败: {e}", exc_info=True)

    return {"id": str(feedback.id), "message": "评价提交成功"}


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
        raise HTTPException(status_code=500, detail=f"问题重写失败: {str(e)}")


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
        raise HTTPException(status_code=500, detail=f"问题分类失败: {str(e)}")


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
        raise HTTPException(status_code=500, detail=f"答案对比失败: {str(e)}")
