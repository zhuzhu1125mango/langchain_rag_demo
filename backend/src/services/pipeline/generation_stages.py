"""生成阶段：答案生成（KB/直答）、token 用量记账（CODE-1 拆分自 stages）。"""

import logging
import time

from src.services.decision_pipeline import QAMode
from src.services.context_builder import estimate_token_count
from src.services.output_sanitizer import OutputSanitizer
from src.services.pipeline.state import (
    KB_ANSWER_TEMPLATE,
    LLM_DIRECT_TEMPLATE,
    _PipelineState,
)
from src.services.reasoning import (
    REASONING_STEP_ANSWER_GENERATE,
)
from src.utils.log_context import log_stage

# 导入 Prometheus 指标模块
try:
    from src.middleware.prometheus import record_kb_query
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

logger = logging.getLogger("rag_system")

from src.utils.error_handling import log_exception

def _kb_source_to_doc(src: dict):
    """把 Agent 工具返回的 KB 来源还原为 Document，供 ContextBuilder 按 kb 类型编号。"""
    from langchain_core.documents import Document as LcDoc

    content = src.get("page_content") or src.get("content") or ""
    return LcDoc(
        page_content=content,
        metadata={
            "document_id": src.get("document_id", ""),
            "filename": src.get("filename") or src.get("source", "unknown"),
            "source": src.get("source", ""),
            "chunk_index": src.get("chunk_index", 0),
            "total_chunks": src.get("total_chunks", 1),
            "kb_id": src.get("kb_id", ""),
            "source_kind": src.get("source_kind") or "raw",
            "score": src.get("score", 0.0),
        },
    )

class GenerationStagesMixin:
    """阶段 9 答案生成，由 PipelineStagesMixin 组合。"""

    @log_stage("generation")
    async def _stage_generate(self, state: _PipelineState):
        """阶段 9：构建最终上下文（ContextBuilder）并流式生成回答。"""
        # Agent 来源是 kb_search + web_search 的混合体，需按 source_type 分流：
        # 统一塞进 web_sources 会把 KB 片段标成 web（context 编号、引用元数据、
        # A/B 的 kb_hit/mrr/recall 全部随之失真）。
        kb_docs_for_context = list(state.docs)
        web_sources_for_context = []
        for src in state.web_sources_for_citation or []:
            if src.get("source_type") == "kb":
                kb_docs_for_context.append(_kb_source_to_doc(src))
            else:
                web_sources_for_context.append(src)
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
            kb_docs=kb_docs_for_context,
            web_sources=web_sources_for_context,
        )
        # 用 ContextBuilder 返回的统一编号来源替换旧的 source_texts/source_metadata
        if numbered_sources:
            state.source_texts = [s["content"] for s in numbered_sources]
            state.source_metadata = numbered_sources

        if final_context:
            # SEC-5：所有检索/网页内容按不可信资料包裹，避免文档内指令被模型执行。
            from src.services.guardrails import (
                contains_injection_signals,
                injection_signal_reason,
                wrap_untrusted_context,
            )

            signal = injection_signal_reason(final_context)
            if signal:
                state.trace.add_stage(
                    "guardrail_context",
                    "suspected",
                    latency_ms=0,
                    detail={"rule": signal},
                )
            safe_context = wrap_untrusted_context(final_context, "retrieved")
            if contains_injection_signals(final_context):
                logger.warning("检索上下文含疑似注入信号，已按不可信资料隔离并记录 trace")
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
                context=safe_context,
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
                if state.verify_holder is not None:
                    # B2-3 流式路径：校验是一次完整 LLM 调用，内联 await 会阻塞
                    # ("final", ...) 事件。此处仅收集输入交给 API 层，在 end 事件后
                    # 后台校验并补发警告 + 落库（见 api/chat.py）。
                    try:
                        state.verify_holder["answer"] = state.final_answer
                        state.verify_holder["citation_sources"] = self._build_citation_sources(
                            state.web_sources_for_citation
                        )
                        state.verify_holder["cross_source_data"] = state.cross_source_data
                    except Exception as exc:
                        log_exception(
                            logger,
                            exc,
                            action="pipeline.verify.build_input",
                            message="构建答案校验输入失败，跳过校验",
                        )
                else:
                    # 非流式路径：无流式 spinner 阻塞问题，仍内联校验并写 trace
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
                    except Exception as exc:
                        log_exception(
                            logger,
                            exc,
                            action="pipeline.verify.stream",
                            message="流式答案校验失败，跳过警告追加",
                        )
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
