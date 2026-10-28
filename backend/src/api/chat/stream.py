"""聊天流式路由：SSE 回答、会话准备与收尾落库。"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from typing import Optional
import asyncio
import uuid
import json
import logging
import time
from datetime import datetime, timezone
from src.database import async_session
from src.auth import get_current_user, CurrentUser, require_owner
from src.models import Session as SessionModel
from src.services.rag_chain import RAGChain
from src.services.reasoning import dedupe_steps
from src.services.vector_store import VectorStoreManager
from src.services.session_service import append_session_message
from src.services.learning_engine import learning_engine
from src.services.pipeline.state import set_ab_config, reset_ab_config
from src.services.title_generator import TitleGenerator
from src.utils.sensitive_words import sensitive_filter
from src.utils.validators import parse_uuid_list, validate_kb_ownership

from .common import (
    MessageRequest,
    chat_quota_dependency,
    _parse_session_id,
    _record_ab_metric,
    _resolve_ab_config,
)

logger = logging.getLogger("src.api.chat")

router = APIRouter(tags=["chat"])

# B2-3：流式收尾（后台答案校验 + 落库）任务引用集，防止 fire-and-forget 任务被 GC。
# 客户端在 end 后断连时该任务仍会完成，保证助手消息（含校验警告）落库。
_stream_finalize_tasks: set = set()

async def _prepare_stream_session(
    session_id: Optional[str],
    question: str,
    kb_ids: Optional[list],
    user_message_id: str,
    current_user: CurrentUser,
) -> Optional[tuple]:
    """获取或创建流式会话并落库用户消息（W6 #4，独立短会话）。

    Returns:
        (current_session_id, history) 元组；session_id 指定的会话不存在时返回 None
        （调用方负责回送 error 事件）。
    """
    async with async_session() as db:
        if session_id:
            result = await db.execute(select(SessionModel).filter(SessionModel.id == _parse_session_id(session_id)))
            session = result.scalar_one_or_none()
            if not session:
                return None
            require_owner(session.user_id, current_user)
            current_session_id = str(session.id)
            history = session.messages.copy()
        else:
            session = SessionModel(
                user_id=current_user.user_id,
                title=question[:50],
                messages=[],
                kb_ids=kb_ids or [],
                updated_at=datetime.now(timezone.utc)
            )
            db.add(session)
            await db.commit()
            await db.refresh(session)
            current_session_id = str(session.id)
            history = []

        # 保存用户消息（原子追加：并发写同一会话不丢消息）
        await append_session_message(db, session.id, {
            "id": user_message_id,
            "role": "user",
            "content": question,
            "timestamp": datetime.now().isoformat()
        })
        await db.commit()
        logger.info(f"用户消息保存成功(流式): session={current_session_id}, message_id={user_message_id}")
        return current_session_id, history

@router.post("/stream", dependencies=[Depends(chat_quota_dependency)])
async def stream_answer(
    request: Request,
    payload: MessageRequest,
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    流式回答接口（SSE，POST body 传参）

    改为 POST 的原因：question 走 GET query 会进入 nginx 访问日志与浏览器历史，
    存在泄露面；且 EventSource 无法携带认证头，POST + fetch 可统一走 X-API-Key。

    Args:
        payload: 请求数据（question 必填，其余可选）
        current_user: 当前认证用户

    Returns:
        StreamingResponse: 流式响应（SSE格式）
    """
    question = payload.question
    session_id = payload.session_id
    kb_ids = payload.kb_ids
    use_web_search = payload.use_web_search
    search_mode = payload.search_mode
    deep_thinking = payload.deep_thinking or "off"

    # request_id 由中间件生成并挂载到 request.state，SSE 错误事件仅回传该 ID
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

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

    # 获取或创建会话 + 落库用户消息（独立短会话；会话不存在时回送 error 事件）
    user_message_id = str(uuid.uuid4())
    assistant_message_id = str(uuid.uuid4())

    prepared = await _prepare_stream_session(session_id, question, kb_ids, user_message_id, current_user)
    if prepared is None:
        async def error_generator():
            yield json.dumps({"type": "error", "error": "会话不存在"})
        return StreamingResponse(error_generator(), media_type="text/event-stream")
    current_session_id, history = prepared

    # 保存助手消息（流式生成结束或异常时调用）。
    # C7：返回会话是否仍为默认标题，标题生成移到 end 事件之后的后台任务，
    # 不再阻塞流完成信号。
    async def save_assistant_message(
        full_answer,
        sources,
        source_metadata,
        reasoning=None,
        thinking="",
        record_learning=True,
        stopped=False,
        error=None,
    ) -> bool:
        # LEARN W-B：记录策略执行样本（短路路径按开关补采各策略结果）。
        # 置于 DB 会话之外——补采会重跑策略投票（含 fast LLM 调用），避免期间占用连接。
        execution_id = None
        if record_learning:
            try:
                decision = rag_chain.get_last_decision()
                if decision:
                    execution_id = await learning_engine.record_chat_execution(
                        session_id=current_session_id,
                        question=question,
                        decision=decision,
                        used_knowledge_base=len(source_metadata) > 0,
                        strategy_manager=rag_chain.strategy_manager,
                        history=history,
                        user_id=current_user.user_id,
                    )
            except Exception as exec_err:
                logger.error(f"记录策略执行失败: {exec_err}", exc_info=True)

        async with async_session() as db:
            result = await db.execute(select(SessionModel).filter(SessionModel.id == uuid.UUID(current_session_id)))
            session = result.scalar_one_or_none()
            if not session:
                logger.warning(f"保存助手消息时未找到会话: {current_session_id}")
                # 会话不存在（可能被并发删除）：无需生成标题。
                # 此前这里 return generated_title，但该变量仅在 send_message（非流式）
                # 作用域定义，stream_answer 内并不存在 → 并发删除会话时会抛 NameError，
                # 被外层 except Exception 吞成"回答已生成但保存失败"。
                return False

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
            if thinking:
                message_payload["thinking"] = thinking
            # 中断/失败状态随消息持久化：刷新或切回会话时仍能复现
            # “已停止/生成失败”，而不是只剩一条孤立的用户问题。
            if stopped:
                message_payload["stopped"] = True
            if error:
                message_payload["error"] = error
            # 原子追加（服务端 jsonb 拼接）
            assistant_msg_count = await append_session_message(db, session.id, message_payload)

            # C7：仅判断是否需要后台生成标题（生成动作移出保存路径，commit 前读取避免属性过期）
            title_generator = await TitleGenerator.get_instance()
            needs_title = title_generator.is_default_title(session.title)

            await db.commit()
            logger.info(f"助手消息保存成功: session={current_session_id}, message_id={assistant_message_id}, content_length={len(full_answer)}, messages_count={assistant_msg_count}")
            return needs_title

    # C7：后台生成会话标题并落库；由 SSE 生成器在 end 事件后调用并补发 title 事件，
    # 客户端提前断开时任务仍会完成（asyncio.shield），标题最终写入数据库
    async def generate_and_save_title() -> Optional[str]:
        async with async_session() as db:
            result = await db.execute(select(SessionModel).filter(SessionModel.id == uuid.UUID(current_session_id)))
            session = result.scalar_one_or_none()
            if not session:
                return None
            title_generator = await TitleGenerator.get_instance()
            if not title_generator.is_default_title(session.title):
                return None
            new_title = None
            try:
                new_title = await title_generator.generate_title(question)
                session.title = new_title
                logger.info(f"会话标题已生成(流式后台): session={current_session_id}, title={new_title}")
            except Exception as title_err:
                logger.warning(f"生成会话标题失败(流式后台): {title_err}")
                return None
            await db.commit()
            return new_title

    # 流式生成回答
    async def generate():
        full_answer = ""
        sources = []
        source_metadata = []
        answer_type = "llm_direct"
        reasoning_steps = []
        thinking_text = ""
        # B2-3：管线把事实校验输入写入此容器（仅含 web 来源时），由本层在 end 后后台校验
        verify_holder: dict = {}

        # LEARN W-C：解析本请求 A/B 变体 → 白名单参数覆盖（含 search_mode）
        ab_config = await _resolve_ab_config(current_user.user_id, current_session_id)
        effective_search_mode = (ab_config or {}).get("search_mode") or (search_mode or "simple")
        ab_token = set_ab_config(ab_config)
        ab_t0 = time.perf_counter()

        try:
            async for result in rag_chain.arun_stream(
                question, kb_ids, history,
                use_web_search=use_web_search,
                search_mode=effective_search_mode,
                user_id=current_user.user_id,
                session_id=current_session_id,
                deep_thinking=deep_thinking,
                verify_holder=verify_holder,
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

                # 模型原始思考增量：转发 thinking 事件供前端折叠面板实时展示，
                # 并累积用于持久化（历史回看）
                if at == "thinking":
                    if chunk:
                        thinking_text += chunk
                        thinking_payload = {'type': 'thinking', 'content': chunk}
                        logger.debug(f"SSE thinking payload: {thinking_payload}")
                        yield f"data: {json.dumps(thinking_payload)}\n\n"
                    continue

                # AM-2 HITL：高危工具审批请求（独立事件类型，前端弹出审批卡片）
                if at == "approval_required":
                    try:
                        approval_payload = json.loads(chunk) if chunk else {}
                    except Exception:
                        approval_payload = {}
                    if approval_payload:
                        logger.info(
                            f"SSE approval_required: tool={approval_payload.get('tool')} "
                            f"id={approval_payload.get('approval_id')}"
                        )
                        yield f"data: {json.dumps(approval_payload, ensure_ascii=False)}\n\n"
                    continue

                # 过滤模型思考阶段产生的空内容，避免前端显示异常
                if chunk:
                    full_answer += chunk
                    content_payload = {'type': 'content', 'content': chunk}
                    # SEC-14：SSE 分片即答案原文，日志只记录长度
                    logger.debug("SSE content chunk: length=%d", len(chunk))
                    yield f"data: {json.dumps(content_payload)}\n\n"
                sources = source_texts
                source_metadata = source_meta
                answer_type = at

        except (asyncio.CancelledError, GeneratorExit):
            # UX-1：用户主动停止 / 客户端断连。始终落库一条助手消息：
            # 有部分内容就保存部分内容，没有内容也保留 stopped 标记，
            # 否则刷新/切回会话后只剩孤立的用户问题。独立任务避免随取消丢失。
            logger.info(f"流式生成被中止，保存部分回答: length={len(full_answer)}")
            partial_task = asyncio.create_task(
                save_assistant_message(
                    full_answer, sources, source_metadata,
                    dedupe_steps(reasoning_steps), thinking_text,
                    record_learning=False, stopped=True,
                )
            )
            _stream_finalize_tasks.add(partial_task)
            partial_task.add_done_callback(_stream_finalize_tasks.discard)
            raise
        except Exception as e:
            logger.error(f"流式生成异常: {str(e)}", exc_info=True)
            # 同样保证助手消息落库：即使一个 token 都没产出，也把失败状态
            # 写进历史，避免刷新后“问题还在、回答消失”。
            error_text = "生成回答失败，请稍后重试"
            try:
                save_task = asyncio.create_task(
                    save_assistant_message(
                        full_answer, sources, source_metadata,
                        dedupe_steps(reasoning_steps), thinking_text,
                        record_learning=False, stopped=True, error=error_text,
                    )
                )
                await asyncio.shield(save_task)
            except Exception as save_err:
                logger.error(f"异常时保存助手消息失败: {save_err}", exc_info=True)
            # 详细错误仅进日志（上方 logger.error 已带 exc_info），客户端只收通用文案 + request_id
            error_payload = {'type': 'error', 'error': '生成回答失败，请稍后重试', 'request_id': request_id}
            logger.debug(f"SSE error payload: {error_payload}")
            yield f"data: {json.dumps(error_payload)}\n\n"
            return
        finally:
            # LEARN W-C：管线消费完毕，复位请求级 A/B 覆盖
            reset_ab_config(ab_token)

        # LEARN W-C：记录系统级实验 response_time 指标（fail-soft）
        await _record_ab_metric(ab_config, (time.perf_counter() - ab_t0) * 1000)

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
                "source_kind": meta.get('source_kind', 'raw'),
            }
            # 网页来源使用标题作为展示名
            if meta.get('source') == 'web_search' or meta.get('url'):
                info["source_type"] = "web"
                if not info["filename"] or info["filename"] == 'web_search':
                    info["filename"] = meta.get('title', '网页来源')
            else:
                info["source_type"] = "kb"
            source_info.append(info)

        # reasoning 按 step 去重：逐事件累积的列表含同一阶段的 running/done 重复条目，
        # 去重后再持久化与下发，避免前端历史渲染出现残留 running 状态（spinner 不消失）
        final_reasoning = dedupe_steps(reasoning_steps)

        # B2-3：事实校验是一次完整 LLM 调用，此前内联在管线里阻塞 ("final", ...)
        # ——答案早已流完，用户还在转圈。现将其连同落库一起交给后台任务：校验 →
        # 补发警告 → 落库（含警告），实时视图与历史回看一致。任务放入集合防 GC，
        # 客户端在 end 后断连时仍会跑完并落库（仅补发的 content 事件丢失）。
        async def verify_and_save() -> tuple:
            suffix = ""
            if verify_holder and rag_chain.answer_verifier is not None:
                try:
                    suffix = await rag_chain.verify_web_answer(
                        verify_holder.get("answer", ""),
                        verify_holder.get("citation_sources") or [],
                        verify_holder.get("cross_source_data"),
                    ) or ""
                except Exception as e:
                    logger.warning(f"答案校验失败，跳过警告追加: {e}")
            try:
                needs = await save_assistant_message(
                    full_answer + suffix, sources, source_metadata, final_reasoning, thinking_text
                )
            except Exception as e:
                logger.error(f"保存助手消息失败: {str(e)}", exc_info=True)
                needs = False
            return suffix, needs

        finalize_task = asyncio.create_task(verify_and_save())
        _stream_finalize_tasks.add(finalize_task)
        finalize_task.add_done_callback(_stream_finalize_tasks.discard)

        # C7：end 事件先发——校验与标题生成均在其后，不再推迟流完成信号
        end_payload = {
            'type': 'end',
            'message_id': assistant_message_id,
            'session_id': current_session_id,
            'sources': source_info,
            'answer_type': answer_type,
            'reasoning': final_reasoning,
        }
        logger.info(f"SSE end payload: {end_payload}")
        yield f"data: {json.dumps(end_payload)}\n\n"

        # 校验完成：补发警告 content 事件（前端 end 后仍会追加显示）
        try:
            suffix, needs_title = await asyncio.shield(finalize_task)
        except Exception:
            suffix, needs_title = "", False
        if suffix:
            logger.debug(f"SSE late verification suffix: {suffix!r}")
            yield f"data: {json.dumps({'type': 'content', 'content': suffix})}\n\n"

        # C7：标题后台生成，完成后补发 title 事件（客户端断开时任务仍落库，
        # 仅 title 事件丢失，侧栏刷新可见新标题）
        if needs_title:
            try:
                title_task = asyncio.create_task(generate_and_save_title())
                new_title = await asyncio.shield(title_task)
                if new_title:
                    title_payload = {'type': 'title', 'session_id': current_session_id, 'title': new_title}
                    logger.debug(f"SSE title payload: {title_payload}")
                    yield f"data: {json.dumps(title_payload)}\n\n"
            except Exception as title_err:
                logger.warning(f"流式后台标题生成失败: {title_err}")

    return StreamingResponse(generate(), media_type="text/event-stream")
