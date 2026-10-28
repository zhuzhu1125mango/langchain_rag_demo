"""管线编排：_pipeline / _pipeline_process（CODE-1 拆分自 stages）。"""

import logging
import time

from src.services.guardrails import (
    INPUT_BLOCK_MESSAGE,
    get_input_guard,
    get_output_guard,
)
from src.services.pipeline.state import (
    _PipelineState,
    _StageTimer,
    _request_retrieval_timings,
    _request_wiki_nav,
    should_think,
)
from src.services.semantic_cache_service import SemanticCacheService
from src.services.identity import build_identity_answer
from src.services.trace_collector import TraceCollector
from src.services.tools.plugins._datetime_impl import build_datetime_answer

# 导入 Prometheus 指标模块
try:
    from src.middleware.prometheus import record_kb_query
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

logger = logging.getLogger("rag_system")

class PipelineOrchestrationMixin:
    """流式/非流式共用的管线编排，由 PipelineStagesMixin 组合。"""

    async def _pipeline(self, question, kb_ids=None, history=None, use_web_search=False, search_mode="simple", user_id=None, session_id=None, deep_thinking="off", verify_holder=None):
        """执行完整问答流程，按事件协议产出 reasoning/chunk/final 事件。

        user_id / session_id 用于 Trace 归属（request_traces 表按用户隔离查询）。
        deep_thinking: 深度思考开关（on/off）。
        verify_holder: B2-3 事实校验输入容器（流式路径由 API 层传入，见 _stage_generate）。
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
            verify_holder=verify_holder,
        )
        state.think = should_think(deep_thinking)
        state.trace = TraceCollector()
        state.trace.set_basic(question=question, session_id=session_id, user_id=user_id)
        # E-1 W3：初始化导航事件缓冲（本任务持有，工具体经共享引用 append；终态汇总写入 trace）
        _request_wiki_nav.set([])

        try:
            # 各阶段执行内部生成器
            output_guard = get_output_guard()
            from src.services.guardrails import StreamingOutputGuard

            stream_guard = StreamingOutputGuard(output_guard)
            recorded_output_rules: set = set()

            def _record_output_hits(hits):
                new_rules = [r for r in hits if r not in recorded_output_rules]
                if new_rules:
                    recorded_output_rules.update(new_rules)
                    logger.warning(f"输出护栏脱敏: rules={new_rules}")
                    state.trace.add_stage(
                        "guardrail_output",
                        "redacted",
                        latency_ms=0,
                        detail={"rules": new_rules},
                    )

            async for ev in self._pipeline_process(state):
                # AM-2 输出护栏：所有 chunk 的统一出口脱敏，保证 SSE/落库/trace 一致。
                # SEC-5：StreamingOutputGuard 额外处理跨 chunk 的密钥/系统提示。
                if ev[0] == "chunk" and ev[1]:
                    redacted, hits = stream_guard.feed(ev[1])
                    if hits:
                        # 阶段在 yield 前已把原文累加进 final_answer，这里同步修正
                        state.final_answer, full_hits = output_guard.redact(state.final_answer)
                        _record_output_hits(hits + full_hits)
                    if not redacted:
                        continue
                    ev = (ev[0], redacted) + tuple(ev[2:])
                elif ev[0] == "final":
                    tail, tail_hits = stream_guard.flush()
                    state.final_answer, full_hits = output_guard.redact(state.final_answer)
                    _record_output_hits(tail_hits + full_hits)
                    if tail:
                        yield (
                            "chunk",
                            tail,
                            state.source_texts,
                            state.source_metadata,
                            state.answer_type or "knowledge_base",
                        )
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
        # AM-2 输入护栏（最先执行）：命中即拒答，不进入改写/检索/生成
        guard_hit = get_input_guard().check(state.question)
        if guard_hit is not None:
            logger.warning(f"输入护栏拦截: rule={guard_hit.rule}")
            state.trace.set_fallback(True, f"guardrail_input:{guard_hit.rule}")
            state.trace.add_stage(
                "guardrail_input",
                "blocked",
                latency_ms=0,
                detail={"kind": guard_hit.kind, "rule": guard_hit.rule},
            )
            state.answer_type = "guardrail_blocked"
            state.final_answer = INPUT_BLOCK_MESSAGE
            yield ("chunk", INPUT_BLOCK_MESSAGE, [], [], "guardrail_blocked")
            async for ev in self._finalize(state):
                yield ev
            return

        # 身份问题直接返回产品口径：小模型无上下文时容易自称 Qwen/阿里云，
        # 固定回答同时避免一次无意义的模型调用。
        identity_answer = build_identity_answer(state.question)
        if identity_answer:
            state.answer_type = "identity"
            state.final_answer = identity_answer
            state.trace.add_stage("identity", "done", latency_ms=0)
            yield ("chunk", identity_answer, [], [], "identity")
            async for ev in self._finalize(state):
                yield ev
            return

        # 阶段 1：上下文增强（指代消解）
        async with _StageTimer(state.trace, "context_enhance"):
            await self._stage_enhance_context(state)

        # 阶段 1.5：精确匹配缓存前置（B2-1）。仅需 normalized question + Redis 单次
        # HGET；命中即回放答案并短路，跳过意图路由/决策/检索/生成全链路。
        # 门控与写入侧一致：需选库、未联网、非深度思考（缓存仅由这类请求写入）。
        if state.kb_ids and not state.use_web_search and state.deep_thinking != "on":
            async with _StageTimer(state.trace, "exact_cache"):
                exact_hit = await SemanticCacheService().lookup_exact(
                    state.user_id or "anonymous", state.kb_ids, state.resolved_question
                )
            if exact_hit is not None:
                async for ev in self._replay_cache_hit(state, exact_hit):
                    yield ev
                async for ev in self._finalize(state):
                    yield ev
                return

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
        async with _StageTimer(state.trace, "kb_retrieval") as _kb_timer:
            async for ev in self._stage_kb_retrieval(state):
                yield ev
            # kb_retrieval 细分耗时（search_hybrid 五步计时）写入 stage detail
            _kb_timings = _request_retrieval_timings.get()
            if _kb_timings:
                _kb_timer.set_detail(_kb_timings)

        # 阶段 9：构建最终上下文并生成回答
        async with _StageTimer(state.trace, "generate"):
            async for ev in self._stage_generate(state):
                yield ev

        async for ev in self._finalize(state):
            yield ev
