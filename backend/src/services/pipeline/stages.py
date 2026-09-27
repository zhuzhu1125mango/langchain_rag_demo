"""统一问答管线各阶段（自 rag_chain.py 拆出的 Mixin）。

包含：_pipeline / _pipeline_process 编排与阶段 1-9 的全部实现
（上下文增强、意图路由、工具优先、KB 决策、语义缓存、Agent、联网搜索、
知识库检索、答案生成）与终态清理。
"""

import asyncio
import logging
import time
from typing import Optional

from src.config import settings
from src.services.decision_pipeline import QAMode
from src.services.context_builder import estimate_token_count
from src.services.intent_router import PrimaryMode
from src.services.output_sanitizer import OutputSanitizer
from src.services.pipeline.state import (
    KB_ANSWER_TEMPLATE,
    LLM_DIRECT_TEMPLATE,
    _PipelineState,
    _StageTimer,
    _background_store_tasks,
    _request_retrieval_score,
    should_think,
)
from src.services.reasoning import (
    REASONING_STEP_ANSWER_GENERATE,
    REASONING_STEP_CACHE_HIT,
    REASONING_STEP_CONTEXT_REWRITE,
    REASONING_STEP_FALLBACK,
    REASONING_STEP_INTENT,
    REASONING_STEP_KB_RETRIEVE,
    REASONING_STEP_TOOL_EXECUTE,
)
from src.services.semantic_cache_service import SemanticCacheService
from src.services.trace_collector import TraceCollector
from src.services.tools.plugins._datetime_impl import build_datetime_answer

logger = logging.getLogger("rag_system")

# 导入 Prometheus 指标模块
try:
    from src.middleware.prometheus import record_kb_query
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False


class PipelineStagesMixin:
    """问答管线编排与各阶段实现（由 RAGChain 组装）。"""

    # ------------------------------------------------------------------
    # 统一问答管线：流式/非流式共用的单一实现。
    # arun_stream() 转发事件给 SSE 消费方；run() 累积 chunk 返回完整结果。
    # ------------------------------------------------------------------
    async def _pipeline(self, question, kb_ids=None, history=None, use_web_search=False, search_mode="simple", user_id=None, session_id=None, deep_thinking="off"):
        """执行完整问答流程，按事件协议产出 reasoning/chunk/final 事件。

        user_id / session_id 用于 Trace 归属（request_traces 表按用户隔离查询）。
        deep_thinking: 深度思考开关（on/off）。
        """
        state = _PipelineState(
            question=question,
            kb_ids=kb_ids,
            history=history,
            use_web_search=use_web_search,
            search_mode=search_mode,
            deep_thinking=deep_thinking,
            user_id=user_id,
            session_id=session_id,
        )
        state.think = should_think(deep_thinking)
        state.trace = TraceCollector()
        state.trace.set_basic(question=question, session_id=session_id, user_id=user_id)

        try:
            # 各阶段执行内部生成器
            async for ev in self._pipeline_process(state):
                yield ev
        finally:
            # SSE 断连兜底：客户端中途断连会 aclose 当前生成器，中断于某条
            # yield 而跳过正常收尾；此处确保 trace 落盘/对话摘要/语义缓存等
            # 持久化副作用仍执行（幂等，正常路径已执行后为 no-op）。
            await self._finalize_side_effects(state)

    async def _pipeline_process(self, state: _PipelineState):
        """问答各阶段执行（内部生成器）。

        供 _pipeline 在外层 try/finally 中迭代；自身可被 aclose 中断，
        终态清理由外层 finally 统一兜底（见 _finalize_side_effects）。
        """
        # 阶段 1：上下文增强（指代消解）
        async with _StageTimer(state.trace, "context_enhance"):
            await self._stage_enhance_context(state)

        # 阶段 2：时间/日期类问题快捷返回
        datetime_start = time.time()
        datetime_answer = build_datetime_answer(state.resolved_question)
        if datetime_answer:
            state.trace.add_stage(
                "datetime_tool", "done", int((time.time() - datetime_start) * 1000)
            )
            if PROMETHEUS_AVAILABLE:
                record_kb_query("datetime_tool")
            state.final_answer = datetime_answer
            yield ("chunk", datetime_answer, [], [], "datetime_tool")
            async for ev in self._finalize(state):
                yield ev
            return

        # 阶段 3：意图路由（含问题改写）
        async with _StageTimer(state.trace, "intent_route"):
            async for ev in self._stage_intent(state):
                yield ev

        # 阶段 4：工具优先（天气/计算等 tool_first 类）
        async with _StageTimer(state.trace, "tool_first"):
            async for ev in self._stage_tool_first(state):
                yield ev
        if state.finished:
            async for ev in self._finalize(state):
                yield ev
            return

        # 阶段 5：知识库使用决策
        async with _StageTimer(state.trace, "kb_decision"):
            await self._stage_decide(state)

        # 阶段 5.5：语义缓存查找（P1-3，仅纯知识库问答路径）
        async with _StageTimer(state.trace, "semantic_cache"):
            async for ev in self._stage_semantic_cache_lookup(state):
                yield ev
        if state.finished:
            async for ev in self._finalize(state):
                yield ev
            return

        # 阶段 6：Agent 模式（纯模式短路 / 混合收集上下文 / Phase 2 降级）
        async with _StageTimer(state.trace, "agent_mode"):
            async for ev in self._stage_agent(state):
                yield ev
        if state.finished:
            async for ev in self._finalize(state):
                yield ev
            return

        # 阶段 7：常规联网搜索（Phase 2）
        async with _StageTimer(state.trace, "web_search"):
            async for ev in self._stage_web_search(state):
                yield ev

        # 阶段 8：知识库检索
        async with _StageTimer(state.trace, "kb_retrieval"):
            async for ev in self._stage_kb_retrieval(state):
                yield ev

        # 阶段 9：构建最终上下文并生成回答
        async with _StageTimer(state.trace, "generate"):
            async for ev in self._stage_generate(state):
                yield ev

        async for ev in self._finalize(state):
            yield ev

    async def _stage_enhance_context(self, state: _PipelineState):
        """阶段 1：上下文增强（指代消解与上下文压缩）。"""
        enhanced = await self.enhance_context(state.question, state.history or [], state.session_id)
        state.resolved_question = enhanced["resolved_question"]
        state.history_context = enhanced["enhanced_context"]
        state.trace.data["resolved_question"] = state.resolved_question

    async def _stage_intent(self, state: _PipelineState):
        """阶段 3：意图路由，tool_first 类问题优先走工具执行。"""
        intent_start = time.time()
        intent_decision = await self.intent_router.route(
            state.resolved_question,
            kb_ids=state.kb_ids,
            use_web_search=state.use_web_search,
            search_mode=state.search_mode,
            history=state.history,
        )
        intent_duration = int((time.time() - intent_start) * 1000)
        logger.info(
            f"意图路由: mode={intent_decision.primary_mode.value}, "
            f"tools={intent_decision.suggested_tools}, reason={intent_decision.reasoning}, "
            f"pipeline={intent_decision.search_pipeline.value}"
        )

        state.intent_decision = intent_decision
        state.trace.set_intent(intent_decision)

        yield ("reasoning", self._reasoning_payload(
            REASONING_STEP_INTENT,
            "done",
            "意图路由",
            content=intent_decision.reasoning or f"识别为 {intent_decision.primary_mode.value}",
            duration_ms=intent_duration,
            metadata={
                "primary_mode": intent_decision.primary_mode.value,
                "suggested_tools": intent_decision.suggested_tools,
                "search_pipeline": intent_decision.search_pipeline.value,
                "needs_realtime": intent_decision.needs_realtime,
            },
        ))

        # 优先使用意图路由层产生的 context_rewrite 作为实际执行问题
        if intent_decision.context_rewrite:
            state.trace.data["context_rewrite"] = intent_decision.context_rewrite
            if intent_decision.context_rewrite != state.resolved_question:
                state.resolved_question = intent_decision.context_rewrite
                logger.info(f"使用意图路由改写后的问题: {state.resolved_question}")
                yield ("reasoning", self._reasoning_payload(
                    REASONING_STEP_CONTEXT_REWRITE,
                    "done",
                    "问题改写",
                    content=f"改写为：{state.resolved_question}",
                    metadata={"rewritten_question": state.resolved_question},
                ))

    async def _stage_tool_first(self, state: _PipelineState):
        """阶段 4：工具优先执行，成功时流式生成答案并短路后续流程。"""
        intent_decision = state.intent_decision
        if intent_decision is None or intent_decision.primary_mode != PrimaryMode.TOOL_FIRST:
            return

        yield ("reasoning", self._reasoning_payload(
            REASONING_STEP_TOOL_EXECUTE,
            "running",
            "工具调用",
            content=f"正在调用 {', '.join(intent_decision.suggested_tools)} ...",
            metadata={"tools": intent_decision.suggested_tools},
        ))

        tool_execute_start = time.time()
        tool_results = await self.tool_executor.execute_with_fallback(
            intent_decision, state.resolved_question, state.history_context
        )
        tool_execute_duration = int((time.time() - tool_execute_start) * 1000)
        state.trace.set_tool_calls(tool_results)
        successful_results = [r for r in tool_results if r.success and r.output]

        if successful_results:
            yield ("reasoning", self._reasoning_payload(
                REASONING_STEP_TOOL_EXECUTE,
                "done",
                "工具调用",
                content=f"工具调用成功：{', '.join(r.tool_name for r in successful_results)}",
                duration_ms=tool_execute_duration,
                metadata={"tools": [r.tool_name for r in successful_results]},
            ))

            # 合并来源信息
            all_sources = []
            for tr in successful_results:
                all_sources.extend(tr.sources or [])
            source_texts, source_metadata = self._web_sources_to_metadata(all_sources)

            async for chunk, _, thinking in self.answer_generator.generate_stream(
                question=state.resolved_question,
                history_context=state.history_context,
                tool_results=successful_results,
                is_realtime=intent_decision.needs_realtime,
                think=state.think,
            ):
                if thinking:
                    yield ("thinking", thinking)
                    continue
                cleaned_chunk, polluted = OutputSanitizer.sanitize(chunk)
                if polluted:
                    state.trace.set_pollution_detected(True)
                if cleaned_chunk:
                    state.final_answer += cleaned_chunk
                    yield ("chunk", cleaned_chunk, source_texts, source_metadata, "tool_first")
            state.finished = True
        else:
            # 工具失败且无降级成功：走后续原有流程
            logger.warning(
                f"工具优先调用失败: {intent_decision.suggested_tools}, 转入原有流程"
            )
            state.trace.set_fallback(True, "工具优先调用失败，转入原有流程")
            yield ("reasoning", self._reasoning_payload(
                REASONING_STEP_TOOL_EXECUTE,
                "failed",
                "工具调用",
                content=f"工具调用失败：{', '.join(intent_decision.suggested_tools)}，转入后续流程",
                duration_ms=tool_execute_duration,
                metadata={"tools": intent_decision.suggested_tools},
            ))
            yield ("reasoning", self._reasoning_payload(
                REASONING_STEP_FALLBACK,
                "done",
                "流程降级",
                content="工具调用失败，转入检索生成流程",
            ))

    async def _stage_decide(self, state: _PipelineState):
        """阶段 5：知识库使用决策（决策结果写入请求级 ContextVar）。"""
        strategy_decision = await self._should_use_knowledge_base(
            state.resolved_question, state.history, state.kb_ids,
            use_web_search=state.use_web_search, search_mode=state.search_mode
        )
        state.decision = self.get_last_decision()
        state.use_kb = strategy_decision
        state.is_agent_mode = bool(
            state.decision
            and state.decision.mode in (QAMode.FUNCTION_CALLING, QAMode.AGENT_SEARCH)
        )

    async def _stage_semantic_cache_lookup(self, state: _PipelineState):
        """阶段 5.5：语义缓存查找（P1-3）。命中时回放缓存答案并短路后续流程。

        仅当 KB 决策判定走纯知识库问答路径（PURE_KB / HYBRID_INTELLIGENT）、
        未开启联网搜索、deep_thinking=off 且非时效性问题时尝试查找。
        Redis/Embedding 异常由服务内部 fail-open，返回 miss 继续正常流程。
        """
        if not settings.semantic_cache.SEMANTIC_CACHE_ENABLED:
            return
        decision = state.decision
        if decision is None or decision.mode not in (QAMode.PURE_KB, QAMode.HYBRID_INTELLIGENT):
            return
        if not state.kb_ids or state.use_web_search or state.deep_thinking == "on":
            return
        if state.intent_decision is not None and state.intent_decision.needs_realtime:
            return

        service = SemanticCacheService()
        hit, embedding = await service.lookup(
            user_id=state.user_id or "anonymous",
            kb_ids=state.kb_ids,
            question=state.resolved_question,
        )
        # 未命中：缓存 embedding 供 finalize 后写入复用，避免二次计算
        state.semantic_cache_embedding = embedding
        if hit is None:
            return

        state.semantic_cache_hit = True
        state.trace.data["semantic_cache_hit"] = {
            "score": round(hit.score, 4),
            "match_type": hit.match_type,
            "cached_question": hit.question,
        }
        yield ("reasoning", self._reasoning_payload(
            REASONING_STEP_CACHE_HIT,
            "done",
            "缓存命中",
            content=f"命中相似问题缓存（相似度 {hit.score:.2f}），直接返回缓存答案",
            metadata={"score": round(hit.score, 4), "match_type": hit.match_type},
        ))

        # 回放缓存答案与来源（事件结构与正常流式一致，前端零改动）
        state.source_texts = list(hit.source_texts)
        state.source_metadata = list(hit.source_metadata)
        state.answer_type = "knowledge_base"
        chunk_chars = max(1, settings.semantic_cache.SEMANTIC_CACHE_REPLAY_CHUNK_CHARS)
        answer = hit.answer or ""
        for i in range(0, len(answer), chunk_chars):
            part = answer[i:i + chunk_chars]
            state.final_answer += part
            yield ("chunk", part, state.source_texts, state.source_metadata, "knowledge_base")
        state.finished = True

    def _maybe_store_semantic_cache(self, state: _PipelineState):
        """finalize 时按条件把纯知识库答案异步写入语义缓存（不阻塞响应）。"""
        if state.semantic_cache_hit or not settings.semantic_cache.SEMANTIC_CACHE_ENABLED:
            return
        if state.answer_type != "knowledge_base" or not state.final_answer:
            return
        if state.web_sources_for_citation or state.search_context:
            return
        if state.decision is None or state.decision.mode not in (QAMode.PURE_KB, QAMode.HYBRID_INTELLIGENT):
            return
        if not state.kb_ids or not state.docs or state.deep_thinking == "on":
            return
        if state.intent_decision is not None and state.intent_decision.needs_realtime:
            return

        service = SemanticCacheService()
        task = asyncio.create_task(service.store(
            user_id=state.user_id or "anonymous",
            kb_ids=state.kb_ids,
            question=state.resolved_question,
            answer=state.final_answer,
            source_texts=state.source_texts,
            source_metadata=state.source_metadata,
            embedding=state.semantic_cache_embedding,
        ))
        # 防止任务被垃圾回收（fire-and-forget）
        _background_store_tasks.add(task)
        task.add_done_callback(_background_store_tasks.discard)

    async def _stage_kb_retrieval(self, state: _PipelineState):
        """阶段 8：知识库检索与相关性评估。"""
        if not (state.use_kb and state.decision and state.decision.mode in (
            QAMode.PURE_KB, QAMode.HYBRID_INTELLIGENT, QAMode.HYBRID_SEARCH,
            QAMode.FUNCTION_CALLING, QAMode.AGENT_SEARCH
        )):
            return

        yield ("reasoning", self._reasoning_payload(
            REASONING_STEP_KB_RETRIEVE,
            "running",
            "知识库检索",
            content="正在检索知识库...",
        ))
        kb_search_start = time.time()
        # P2-2：多查询改写（开关开启时），改写失败自动回退单查询
        queries = None
        if settings.processing.KB_MULTI_QUERY_ENABLED:
            try:
                rewritten = await self.query_rewriter.rewrite(state.resolved_question)
                if rewritten and len(rewritten) > 1:
                    queries = rewritten
            except Exception as e:
                logger.warning(f"KB 多查询改写失败，回退单查询: {e}")
        state.docs = await self._retrieve_documents(
            state.resolved_question, state.kb_ids,
            query_embedding=state.semantic_cache_embedding,
            queries=queries,
        )

        kb_search_duration = int((time.time() - kb_search_start) * 1000)
        if state.docs and len(state.docs) > 0:
            _retrieval_score, has_relevant = await self._calculate_relevance(state.resolved_question, state.docs)
            _request_retrieval_score.set(_retrieval_score)

            if has_relevant:
                # P3 交叉链接检索扩展（rerank 后、context 组装前追加，开关控制）
                if settings.wiki_compile.WIKI_LINK_EXPANSION:
                    from src.services.wiki_link_expansion import expand_wiki_links

                    state.docs = await expand_wiki_links(state.docs)
                doc_texts, doc_metadata = self._extract_source_info(state.docs)
                state.source_texts.extend(doc_texts)
                state.source_metadata.extend(doc_metadata)
                retrieve_metadata = {"sources_count": len(doc_metadata)}
                if queries:
                    retrieve_metadata["multi_query_count"] = len(queries)
                yield ("reasoning", self._reasoning_payload(
                    REASONING_STEP_KB_RETRIEVE,
                    "done",
                    "知识库检索",
                    content=f"知识库检索完成，命中 {len(doc_metadata)} 个片段",
                    duration_ms=kb_search_duration,
                    metadata=retrieve_metadata,
                ))
            else:
                state.docs = []
                yield ("reasoning", self._reasoning_payload(
                    REASONING_STEP_KB_RETRIEVE,
                    "done",
                    "知识库检索",
                    content="知识库检索完成，未找到高度相关内容",
                    duration_ms=kb_search_duration,
                    metadata={"sources_count": 0},
                ))
        else:
            yield ("reasoning", self._reasoning_payload(
                REASONING_STEP_KB_RETRIEVE,
                "done",
                "知识库检索",
                content="知识库检索完成，未找到相关内容",
                duration_ms=kb_search_duration,
                metadata={"sources_count": 0},
            ))

    def _record_token_usage(self, state: _PipelineState, prompt: str):
        """把 token 用量写入 trace：优先 LLM 元数据，缺失时字符估算兜底（P1-2）。"""
        if state.token_usage_recorded:
            return
        state.token_usage_recorded = True
        meta = state.llm_token_meta or {}
        prompt_tokens = meta.get("prompt_tokens")
        completion_tokens = meta.get("completion_tokens")
        if prompt_tokens or completion_tokens:
            state.trace.set_token_usage(prompt_tokens, completion_tokens, estimated=False)
        else:
            state.trace.set_token_usage(
                estimate_token_count(prompt),
                estimate_token_count(state.final_answer),
                estimated=True,
            )

    async def _stage_generate(self, state: _PipelineState):
        """阶段 9：构建最终上下文（ContextBuilder）并流式生成回答。"""
        web_sources_for_context = list(state.web_sources_for_citation) if state.web_sources_for_citation else []
        if state.search_context and not web_sources_for_context:
            # Agent 等场景可能只返回格式化上下文字符串，构造一条合成来源
            web_sources_for_context.append({
                "title": "联网搜索结果",
                "content": state.search_context,
                "url": "",
                "source": "web_search",
            })

        final_context, numbered_sources = self.context_builder.build_context(
            question=state.resolved_question,
            kb_docs=state.docs,
            web_sources=web_sources_for_context,
        )
        # 用 ContextBuilder 返回的统一编号来源替换旧的 source_texts/source_metadata
        if numbered_sources:
            state.source_texts = [s["content"] for s in numbered_sources]
            state.source_metadata = numbered_sources

        if final_context:
            if PROMETHEUS_AVAILABLE:
                if state.search_context and state.docs:
                    record_kb_query("hybrid_search")
                elif state.search_context:
                    record_kb_query("web_search")
                else:
                    record_kb_query("knowledge_base")

            answer_type = "web_search" if state.search_context else "knowledge_base"
            if state.is_agent_mode:
                answer_type = "function_calling" if state.decision.mode == QAMode.FUNCTION_CALLING else "agent_search"
            elif state.search_context and state.docs:
                answer_type = "hybrid_search"
            state.answer_type = answer_type

            prompt = KB_ANSWER_TEMPLATE.format(
                history=state.history_context,
                context=final_context,
                question=state.resolved_question,
            )
            answer_generate_start = time.time()
            yield ("reasoning", self._reasoning_payload(
                REASONING_STEP_ANSWER_GENERATE,
                "running",
                "生成回答",
                content="正在生成回答...",
            ))
            async for chunk, source_texts, source_metadata, at in self._stream_with_retry(
                prompt, state.source_texts, state.source_metadata, answer_type, state=state, think=state.think
            ):
                if at == "thinking":
                    yield ("thinking", chunk)
                    continue
                cleaned_chunk, polluted = OutputSanitizer.sanitize(chunk)
                if polluted:
                    state.trace.set_pollution_detected(True)
                if cleaned_chunk:
                    state.final_answer += cleaned_chunk
                    yield ("chunk", cleaned_chunk, source_texts, source_metadata, at)
            self._record_token_usage(state, prompt)
            answer_generate_duration = int((time.time() - answer_generate_start) * 1000)
            yield ("reasoning", self._reasoning_payload(
                REASONING_STEP_ANSWER_GENERATE,
                "done",
                "生成回答",
                content="回答生成完成",
                duration_ms=answer_generate_duration,
            ))
            # 含 web 来源时做事实校验：追加警告后缀（引用补全仅非流式在生成前做）
            if state.web_sources_for_citation and self.answer_verifier is not None:
                try:
                    citation_sources = self._build_citation_sources(state.web_sources_for_citation)
                    suffix, verification_result = await self._verify_answer_suffix(
                        state.final_answer, citation_sources, state.cross_source_data
                    )
                    if suffix:
                        state.final_answer += suffix
                        yield ("chunk", suffix, state.source_texts, state.source_metadata, answer_type)
                    if verification_result is not None:
                        state.trace.data["verification"] = {
                            "confidence": verification_result.confidence,
                            "is_consistent": verification_result.is_consistent,
                            "warnings": verification_result.warnings,
                        }
                except Exception as e:
                    logger.warning(f"流式答案校验失败，跳过警告追加: {e}")
        else:
            if PROMETHEUS_AVAILABLE:
                record_kb_query("llm_direct")

            state.answer_type = "llm_direct"
            prompt = LLM_DIRECT_TEMPLATE.format(
                history=state.history_context,
                question=state.resolved_question,
            )
            answer_generate_start = time.time()
            yield ("reasoning", self._reasoning_payload(
                REASONING_STEP_ANSWER_GENERATE,
                "running",
                "生成回答",
                content="正在生成回答...",
            ))
            async for chunk, source_texts, source_metadata, at in self._stream_with_retry(
                prompt, [], [], "llm_direct", state=state, think=state.think
            ):
                if at == "thinking":
                    yield ("thinking", chunk)
                    continue
                cleaned_chunk, polluted = OutputSanitizer.sanitize(chunk)
                if polluted:
                    state.trace.set_pollution_detected(True)
                if cleaned_chunk:
                    state.final_answer += cleaned_chunk
                    yield ("chunk", cleaned_chunk, source_texts, source_metadata, at)
            self._record_token_usage(state, prompt)
            answer_generate_duration = int((time.time() - answer_generate_start) * 1000)
            yield ("reasoning", self._reasoning_payload(
                REASONING_STEP_ANSWER_GENERATE,
                "done",
                "生成回答",
                content="回答生成完成",
                duration_ms=answer_generate_duration,
            ))

    async def _finalize_side_effects(self, state: _PipelineState):
        """终态清理副作用：语义缓存写入 + 链路落盘 + 对话摘要更新。

        与流式 ("final", ...) 事件解耦，使 SSE 客户端中途断连（生成器被
        aclose 关闭而中断于某条 yield）时，这些持久化操作仍会执行，而非
        连同 final 事件一起被丢弃。幂等：state.finalized 防重复执行。
        """
        if state.finalized:
            return
        state.finalized = True
        try:
            # P1-3：符合条件的纯知识库答案异步写入语义缓存
            self._maybe_store_semantic_cache(state)
            # 阶段一 D3：答案回流为 wiki synthesis 页（WIKI_SYNTHESIS_ENABLED 灰度，
            # 内部去抖+best-effort，绝不阻断响应）
            try:
                from src.services.wiki_synthesis_service import maybe_schedule_synthesis

                await maybe_schedule_synthesis(state)
            except Exception as e:
                logger.warning(f"synthesis 回流调度失败（忽略）: {e}")
            state.trace.set_final_answer(state.final_answer)
            state.trace.finish()
            state.trace.save_background()
            await self._update_conversation_summary(state.history or [], state.session_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 收尾为最佳努力（best-effort），异常不应向客户端回传或阻塞断连流程
            logger.warning(f"终态清理异常: {e}", exc_info=True)

    async def _finalize(self, state: _PipelineState):
        """终态：执行清理副作用并产出 ("final", state) 事件。"""
        await self._finalize_side_effects(state)
        yield ("final", state)
