"""Agent 与联网搜索相关方法（自 rag_chain.py 拆出的 Mixin）。

包含：L1-a 跨请求会话记忆、Function Calling / ReAct Agent 执行、
P2 有界 Agent 循环编排衔接、Agent 降级 Phase 2 联网搜索与来源格式转换。
"""

import logging
import time
from typing import Dict, List

from src.config import settings
from src.services.decision_pipeline import QAMode
from src.services.output_sanitizer import OutputSanitizer
from src.services.pipeline.state import _PipelineState, _memory_doc_id
from src.services.reasoning import REASONING_STEP_WEB_SEARCH
from src.services.search_agent import (
    FunctionCallingHandler,
    ReActAgent,
    SearchAgentResult,
    SearchToolkit,
)

logger = logging.getLogger("rag_system")


class AgentMixin:
    """Agent / 联网搜索方法集（由 RAGChain 组装）。"""

    # ------------------------------------------------------------------
    # L1-a 跨请求记忆（AGENT_MEMORY_ENABLED 灰度，见 agent-evolution.md §11.2）
    # ------------------------------------------------------------------
    async def _load_agent_memory(self, question: str, session_id: str, top_k: int = 3) -> str:
        """检索当前会话与"当前问题"相关的历史记忆，拼接注入决策 prompt（fail-open）。

        记忆以 UUIDv5(session_id) 作 document_id + source_kind="memory" 写入主 collection，
        同会话多条记忆聚合在同一 document_id，检索按该 document_id 隔离会话，
        并以 question 作语义 query 取最相关的历史记忆。任何异常不阻断主流程。
        """
        if not session_id or not settings.search.AGENT_MEMORY_ENABLED:
            return ""
        if not self._has_vector_store():
            return ""
        try:
            doc_id = _memory_doc_id(session_id)
            results = await self.vector_store.search_hybrid(
                question, k=top_k, document_ids=[doc_id], source_kind="memory"
            )
            if not results:
                return ""
            # search_hybrid 返回 Document；取 page_content 倒序拼接（默认近写入优先，倒序成时间正序）
            mems = [d.page_content for d in results if getattr(d, "page_content", "")]
            return "\n\n".join(mems[::-1]) if mems else ""
        except Exception as e:
            logger.warning(f"加载 Agent 记忆失败（忽略）: {e}")
            return ""

    async def _save_agent_memory(self, session_id: str, summary: str) -> None:
        """将本轮发言摘要异步写入会话记忆（fail-open）。

        summary 空则跳过；复用 insert_embeddings 走既有 embedding+写入管线。
        """
        if not session_id or not summary or not settings.search.AGENT_MEMORY_ENABLED:
            return
        if not self._has_vector_store():
            return
        try:
            from langchain_core.documents import Document as LcDoc
            doc_id = _memory_doc_id(session_id)
            doc = LcDoc(
                page_content=summary[:2000],
                metadata={
                    "document_id": doc_id,
                    "source": "对话记忆",
                    "chunk_index": 0,
                    "source_kind": "memory",
                },
            )
            await self.vector_store.milvus_service.insert_embeddings(
                [doc], kb_id=f"memory"
            )
        except Exception as e:
            logger.warning(f"写入 Agent 记忆失败（忽略）: {e}")

    # ------------------------------------------------------------------
    # Phase 3: Agent 搜索辅助方法
    # ------------------------------------------------------------------
    def _ensure_agents(self):
        """按需初始化 Function Calling / ReAct Agent"""
        if self.search_toolkit is None:
            self.search_toolkit = SearchToolkit(self.web_search_service)

        if self.function_calling_handler is None:
            self.function_calling_handler = FunctionCallingHandler(self.llm, self.search_toolkit)

        if self.react_agent is None:
            self.react_agent = ReActAgent(self.llm, self.search_toolkit)

    def _web_sources_to_metadata(self, sources: List[Dict]) -> tuple:
        """将 Agent 返回的 web 来源转换为 RAGChain 统一格式"""
        source_texts = []
        source_metadata = []
        for s in sources:
            page_content = s.get("page_content", "")
            source_texts.append(page_content)
            source_metadata.append({
                "document_id": s.get("document_id", ""),
                "filename": s.get("filename", "web_search"),
                "source": s.get("source", ""),
                "chunk_index": s.get("chunk_index", 0),
                "total_chunks": s.get("total_chunks", 1),
                "page_content": page_content,
                "url": s.get("url", ""),
            })
        return source_texts, source_metadata

    def _build_citation_sources(self, web_sources: List[Dict]) -> List[Dict]:
        """将 web 搜索来源转换为 CitationBackfiller/AnswerVerifier 所需格式。

        CitationBackfiller 期望 sources 含 source_index/title/content 字段；
        build_search_context_enhanced 返回的 sources 已含这些字段，
        此方法用于兼容 build_search_context_with_sources 或 Agent 来源。

        Args:
            web_sources: web 搜索来源列表。

        Returns:
            含 source_index/title/content/url 的来源列表。
        """
        citation_sources = []
        for i, s in enumerate(web_sources or [], 1):
            content = s.get("content") or s.get("page_content", "")
            citation_sources.append({
                "source_index": s.get("source_index", i),
                "title": s.get("title", ""),
                "content": content,
                "url": s.get("url", ""),
            })
        return citation_sources

    async def _run_agent(self, question: str, history_context: str, mode: QAMode) -> SearchAgentResult:
        """非流式执行 Agent"""
        self._ensure_agents()
        try:
            if mode == QAMode.FUNCTION_CALLING:
                return await self.function_calling_handler.run(question, history_context)
            if mode == QAMode.AGENT_SEARCH:
                return await self.react_agent.run(question, history_context)
        except Exception as e:
            logger.warning(f"Agent 执行失败 [{mode.value}]: {e}")
        return SearchAgentResult(answer="")

    async def _stream_agent(self, question: str, history_context: str, mode: QAMode):
        """流式执行 Agent，yield (chunk, sources)"""
        self._ensure_agents()
        try:
            if mode == QAMode.FUNCTION_CALLING:
                async for chunk, sources in self.function_calling_handler.arun_stream(question, history_context):
                    yield chunk, sources
                return
            if mode == QAMode.AGENT_SEARCH:
                async for chunk, sources in self.react_agent.arun_stream(question, history_context):
                    yield chunk, sources
                return
        except Exception as e:
            logger.warning(f"Agent 流式执行失败 [{mode.value}]: {e}")
        yield "", []

    async def _stage_agent(self, state: _PipelineState):
        """阶段 6：Agent 模式（Function Calling / ReAct）。

        纯 Agent 模式：流式输出 Agent 回答，空输出或被工具 JSON 污染时
        降级到 Phase 2 联网搜索；Agent + 知识库混合：先收集 web 上下文，
        再与知识库合并生成。
        """
        if not state.is_agent_mode:
            return

        # P2：有界 Agent 循环（灰度开关 AGENT_ORCHESTRATOR_ENABLED 控制，
        # 初始化失败或未开启时回退旧 FunctionCalling/ReAct 分支）
        if settings.search.AGENT_ORCHESTRATOR_ENABLED and self.agent_orchestrator is not None:
            async for ev in self._run_agent_orchestrator(state):
                yield ev
            return

        agent_sources = []

        if not state.use_kb:
            # 纯 Agent 模式：直接流式输出 Agent 生成的回答
            answer_type = (
                "function_calling"
                if state.decision.mode == QAMode.FUNCTION_CALLING
                else "agent_search"
            )
            state.answer_type = answer_type
            async for chunk, agent_src in self._stream_agent(
                state.resolved_question, state.history_context, state.decision.mode
            ):
                if chunk:
                    state.final_answer += chunk
                    yield ("chunk", chunk, state.source_texts, state.source_metadata, answer_type)
                agent_sources = agent_src

            # 方案B：Agent 未调用工具（输出为空且无 sources），或 Agent 输出被工具 JSON 污染
            # （推理模型容易把工具调用 JSON 直接当回答输出），降级到 Phase 2 联网搜索。
            # 适用于时效性问题 Agent 决策失败的场景（如"吕梁天气"未触发 web_search），
            # 确保实时信息一定被搜索，而非由 LLM 笼统回复"无法获取"或输出工具 JSON。
            from src.services.search_agent import looks_like_tool_call
            answer_polluted = looks_like_tool_call(state.final_answer)
            if (not state.final_answer and not agent_sources or answer_polluted) and settings.search.SEARCH_AGENT_FALLBACK_TO_PHASE2:
                if answer_polluted:
                    logger.warning(f"Agent 回答被工具 JSON 污染，降级到 Phase 2 联网搜索: {state.resolved_question}")
                else:
                    logger.info(f"Agent 输出为空，降级到 Phase 2 联网搜索: {state.resolved_question}")
                async for ev in self._phase2_web_search(state):
                    yield ev
                # 不终止，继续走后续 final_context 构建与回答生成逻辑。
                # 重置 is_agent_mode 让 answer_type 正确显示为 web_search。
                state.is_agent_mode = False
            else:
                state.source_texts, state.source_metadata = self._web_sources_to_metadata(agent_sources)
                # 阶段三：保存 Agent 链路（trace 记录清洗后的完整回答）
                cleaned_answer, polluted = OutputSanitizer.sanitize(state.final_answer)
                state.final_answer = cleaned_answer
                state.trace.set_pollution_detected(polluted)
                state.finished = True
            if state.finished:
                return

        # Agent + 知识库混合：先让 Agent 收集 web 上下文，再与知识库合并生成
        agent_result = await self._run_agent(state.resolved_question, state.history_context, state.decision.mode)
        if agent_result.answer or agent_result.context:
            state.search_context = agent_result.context or agent_result.answer
            agent_sources = agent_result.sources
        elif settings.search.SEARCH_AGENT_FALLBACK_TO_PHASE2:
            # 方案B：Agent 未调用工具时降级到 Phase 2 联网搜索
            logger.info(f"Agent 输出为空，降级到 Phase 2 联网搜索: {state.resolved_question}")
            async for ev in self._phase2_web_search(state):
                yield ev
            state.is_agent_mode = False

        if not state.source_texts and not state.source_metadata and agent_sources:
            state.source_texts, state.source_metadata = self._web_sources_to_metadata(agent_sources)
            state.web_sources_for_citation = agent_sources
        logger.info(
            f"Agent 搜索已触发: mode={state.decision.mode.value}, query={state.resolved_question}, "
            f"context_length={len(state.search_context)}, sources={len(state.source_metadata)}"
        )

    async def _run_agent_orchestrator(self, state: _PipelineState):
        """P2：有界 Agent 循环的事件映射与降级衔接。

        纯 Agent 模式（use_kb=False）：循环综合答案直接流式输出；
        空输出或污染时降级 Phase 2 联网搜索（与旧路径语义一致）。
        混合模式（use_kb=True）：循环仅收集工具上下文，交给阶段 8/9 合并生成。
        """
        from src.services.search_agent import looks_like_tool_call

        answer_type = (
            "function_calling"
            if state.decision.mode == QAMode.FUNCTION_CALLING
            else "agent_search"
        )
        state.answer_type = answer_type

        result = None
        # L1-a：读取本会话历史记忆，注入决策 prompt（fail-open，空串即关闭）
        memory_context = ""
        if settings.search.AGENT_MEMORY_ENABLED and state.session_id:
            memory_context = await self._load_agent_memory(
                state.resolved_question, state.session_id,
                top_k=settings.search.AGENT_MEMORY_TOP_K,
            )
        async for ev in self.agent_orchestrator.run_stream(
            question=state.resolved_question,
            history_context=state.history_context,
            kb_ids=state.kb_ids,
            collect_only=bool(state.use_kb),
            think=bool(state.think),
            answer_type=answer_type,
            memory_context=memory_context,
            owner_id=state.user_id,
        ):
            kind = ev[0]
            if kind in ("reasoning", "thinking"):
                yield ev
            elif kind == "chunk":
                _, chunk, src_texts, src_meta, _atype = ev
                if chunk:
                    state.final_answer += chunk
                    if src_texts and not state.source_texts:
                        state.source_texts = src_texts
                    if src_meta and not state.source_metadata:
                        state.source_metadata = src_meta
                    yield ev
            elif kind == "result":
                result = ev[1]

        if result is None:
            # 编排器异常中断（不应发生），回退 Phase 2 搜索
            async for ev2 in self._phase2_web_search(state):
                yield ev2
            state.is_agent_mode = False
            return

        # L1-a：写入本会话记忆（fail-open）。摘要 = 问答对 + 关键工具观察，
        # 简洁文本避免额外 LLM 调用；异步不阻塞响应。
        if settings.search.AGENT_MEMORY_ENABLED and state.session_id:
            q = state.resolved_question.strip()
            a = (state.final_answer or "").strip()
            if q and a:
                summary = f"问：{q}\n答：{a}"
                try:
                    await self._save_agent_memory(state.session_id, summary)
                except Exception as e:
                    logger.warning(f"Agent 记忆写入（兜底）失败（忽略）: {e}")

        if not state.use_kb:
            # 纯 Agent 模式：空答案或污染 → 降级 Phase 2
            polluted = bool(result.get("polluted")) or looks_like_tool_call(state.final_answer)
            if polluted:
                state.trace.set_pollution_detected(True)
            if (not state.final_answer or polluted) and settings.search.SEARCH_AGENT_FALLBACK_TO_PHASE2:
                if polluted:
                    logger.warning(f"Agent 循环回答被污染，降级到 Phase 2: {state.resolved_question}")
                else:
                    logger.info(f"Agent 循环输出为空，降级到 Phase 2: {state.resolved_question}")
                async for ev2 in self._phase2_web_search(state):
                    yield ev2
                state.is_agent_mode = False
                return

            sources = result.get("sources") or []
            if sources and not state.source_metadata:
                state.source_texts, state.source_metadata = self._web_sources_to_metadata(sources)
                state.web_sources_for_citation = sources
            cleaned, pol = OutputSanitizer.sanitize(state.final_answer)
            state.final_answer = cleaned
            state.trace.set_pollution_detected(pol)
            state.finished = True
            logger.info(
                f"Agent 循环完成: steps={result.get('steps')}, reason={result.get('reason')}, "
                f"answer_len={len(state.final_answer)}"
            )
            return

        # 混合模式：工具上下文并入 search_context，交由阶段 8/9 合并生成
        context = result.get("context") or result.get("answer") or ""
        if context:
            state.search_context = context
            sources = result.get("sources") or []
            if sources and not state.source_texts and not state.source_metadata:
                state.source_texts, state.source_metadata = self._web_sources_to_metadata(sources)
                state.web_sources_for_citation = sources
            logger.info(
                f"Agent 循环上下文已收集: steps={result.get('steps')}, "
                f"context_length={len(context)}, sources={len(state.source_metadata)}"
            )
        elif settings.search.SEARCH_AGENT_FALLBACK_TO_PHASE2:
            logger.info(f"Agent 循环未获得上下文，降级到 Phase 2: {state.resolved_question}")
            async for ev2 in self._phase2_web_search(state):
                yield ev2
            state.is_agent_mode = False

    async def _phase2_web_search(self, state: _PipelineState):
        """Agent 降级路径的 Phase 2 联网搜索（含 reasoning 事件）。"""
        web_search_start = time.time()
        yield ("reasoning", self._reasoning_payload(
            REASONING_STEP_WEB_SEARCH,
            "running",
            "联网搜索",
            content="正在联网搜索...",
        ))
        try:
            search_context, web_sources, cross_source_data = await self.web_search_service.build_search_context_enhanced(
                state.resolved_question, conversation_context=state.history or []
            )
            state.search_context = search_context
            state.cross_source_data = cross_source_data
            web_search_duration = int((time.time() - web_search_start) * 1000)
            if web_sources:
                web_texts, web_metadata = self._web_sources_to_metadata(web_sources)
                state.source_texts.extend(web_texts)
                state.source_metadata.extend(web_metadata)
                state.web_sources_for_citation = web_sources
                yield ("reasoning", self._reasoning_payload(
                    REASONING_STEP_WEB_SEARCH,
                    "done",
                    "联网搜索",
                    content=f"联网搜索完成，找到 {len(web_sources)} 个来源",
                    duration_ms=web_search_duration,
                    metadata={"sources_count": len(web_sources)},
                ))
            else:
                yield ("reasoning", self._reasoning_payload(
                    REASONING_STEP_WEB_SEARCH,
                    "failed",
                    "联网搜索",
                    content="未找到相关网络结果",
                    duration_ms=web_search_duration,
                ))
        except Exception as e:
            logger.warning(f"Agent 降级搜索失败: {e}")
            yield ("reasoning", self._reasoning_payload(
                REASONING_STEP_WEB_SEARCH,
                "failed",
                "联网搜索",
                content="联网搜索服务不可用",
            ))

    async def _stage_web_search(self, state: _PipelineState):
        """阶段 7：常规联网搜索模式（Phase 2）。"""
        if not (state.decision and state.decision.mode in (QAMode.WEB_SEARCH, QAMode.HYBRID_SEARCH)):
            return

        web_search_start = time.time()
        yield ("reasoning", self._reasoning_payload(
            REASONING_STEP_WEB_SEARCH,
            "running",
            "联网搜索",
            content="正在联网搜索...",
        ))
        try:
            search_context, web_sources, cross_source_data = await self.web_search_service.build_search_context_enhanced(
                state.resolved_question, conversation_context=state.history or []
            )
            state.search_context = search_context
            state.cross_source_data = cross_source_data
            web_search_duration = int((time.time() - web_search_start) * 1000)
            if web_sources:
                web_texts, web_metadata = self._web_sources_to_metadata(web_sources)
                state.source_texts.extend(web_texts)
                state.source_metadata.extend(web_metadata)
                state.web_sources_for_citation = web_sources
                yield ("reasoning", self._reasoning_payload(
                    REASONING_STEP_WEB_SEARCH,
                    "done",
                    "联网搜索",
                    content=f"联网搜索完成，找到 {len(web_sources)} 个来源",
                    duration_ms=web_search_duration,
                    metadata={"sources_count": len(web_sources)},
                ))
            else:
                yield ("reasoning", self._reasoning_payload(
                    REASONING_STEP_WEB_SEARCH,
                    "failed",
                    "联网搜索",
                    content="未找到相关网络结果",
                    duration_ms=web_search_duration,
                ))
            logger.info(
                f"联网搜索已触发: mode={state.decision.mode.value}, query={state.resolved_question}, "
                f"context_length={len(state.search_context)}, sources={len(web_sources)}"
            )
        except Exception as e:
            logger.warning(f"联网搜索失败: {e}")
            yield ("reasoning", self._reasoning_payload(
                REASONING_STEP_WEB_SEARCH,
                "failed",
                "联网搜索",
                content="联网搜索服务不可用",
            ))
