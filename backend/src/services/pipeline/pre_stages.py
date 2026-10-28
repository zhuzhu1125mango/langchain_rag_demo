"""前段阶段：上下文增强、意图路由、工具优先、KB 决策（CODE-1 拆分自 stages）。"""

import logging
import time

from src.services.decision_pipeline import QAMode
from src.services.intent_router import PrimaryMode
from src.services.output_sanitizer import OutputSanitizer
from src.services.pipeline.state import (
    _PipelineState,
)
from src.services.reasoning import (
    REASONING_STEP_CONTEXT_REWRITE,
    REASONING_STEP_FALLBACK,
    REASONING_STEP_INTENT,
    REASONING_STEP_TOOL_EXECUTE,
)
from src.utils.security import redact_for_log
from src.utils.log_context import log_stage

logger = logging.getLogger("rag_system")

class PreStagesMixin:
    """阶段 1-3 与 KB 决策，由 PipelineStagesMixin 组合。"""

    @log_stage("enhance_context")
    async def _stage_enhance_context(self, state: _PipelineState):
        """阶段 1：上下文增强（指代消解与上下文压缩）。"""
        enhanced = await self.enhance_context(state.question, state.history or [], state.session_id)
        state.resolved_question = enhanced["resolved_question"]
        state.history_context = enhanced["enhanced_context"]
        state.trace.data["resolved_question"] = state.resolved_question

    @log_stage("intent")

    @log_stage("intent")
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
        # （context_rewrite 已随 intent_decision.to_dict() 落入 trace，无需另存顶层键）
        if intent_decision.context_rewrite:
            if intent_decision.context_rewrite != state.resolved_question:
                state.resolved_question = intent_decision.context_rewrite
                logger.info(f"使用意图路由改写后的问题: {redact_for_log(state.resolved_question)}")
                yield ("reasoning", self._reasoning_payload(
                    REASONING_STEP_CONTEXT_REWRITE,
                    "done",
                    "问题改写",
                    content=f"改写为：{state.resolved_question}",
                    metadata={"rewritten_question": state.resolved_question},
                ))

    @log_stage("tool_first")

    @log_stage("tool_first")
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
            # 同步到 state：流末尾 OutputGuard flush 补发的 tail chunk 会读取
            # state.source_* / answer_type，未同步会导致 end 事件来源被清空、
            # 回答类型被回退成 llm_direct。
            state.source_texts = source_texts
            state.source_metadata = source_metadata
            state.answer_type = "tool_first"

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

    @log_stage("decide")

    @log_stage("decide")
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
