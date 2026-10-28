"""Agent 流式主循环：DECIDE/ACT/OBSERVE 循环、综合生成与事件输出（CODE-1 拆分）。"""

import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from src.config import settings

logger = logging.getLogger("rag_system")

from src.utils.error_handling import log_exception

from src.services.agent_state import AgentLoopState

class AgentStreamMixin:
    """有界循环与事件流，由 AgentOrchestrator 组合。"""

    async def run_stream(
        self,
        question: str,
        history_context: str = "",
        kb_ids: Optional[List[str]] = None,
        collect_only: bool = False,
        think: bool = False,
        answer_type: str = "agent_orchestrator",
        memory_context: str = "",
        owner_id: Optional[str] = None,
        needs_web: bool = False,
        trace: Any = None,
    ) -> AsyncIterator[Tuple]:
        """有界循环 + 综合。

        Args:
            question: 当前问题（已消解）。
            collect_only: True = 混合模式（仅收集上下文，交给现有生成阶段）；
                          False = 纯 Agent 模式（循环结束后流式综合输出）。
            think: 综合阶段是否启用深度思考（deep_thinking 开关透传）。
            memory_context: 跨请求记忆（L1-a）。由外部（rag_chain）检索当前会话的历史
                          摘要注入，附加到决策 prompt，实现会话级记忆。
            owner_id: 当前用户 ID（资源 owner）。透传给 kb_search/wiki_lookup
                      做工具侧 kb 归属校验（安全 W1-4，纵深防御）。
            needs_web: 混合问题标志（路由 `needs_kb and needs_web`）。为 True 时决策
                      prompt 注入「kb+web 双源」引导，避免只调 kb_search 就作答。
            trace: 可选 TraceCollector，用于记录 HITL 审批事件（AM-2）。
        """
        # perf_counter：单调时钟，避免 time.time() 在 Windows 上的粗粒度/回退问题
        loop = AgentLoopState(
            deadline=time.perf_counter() + settings.search.AGENT_TIME_BUDGET_MS / 1000.0,
            needs_web=needs_web,
        )
        all_results = []
        direct_answer = ""
        end_reason = "max_steps"

        for step in range(1, self.max_steps + 1):
            if time.perf_counter() >= loop.deadline:
                end_reason = "time_budget"
                break

            # ---- DECIDE ----
            try:
                if step == 1 and settings.search.AGENT_PLAN_ENABLED:
                    # L2 V2：规划并入首步单次调用（正文产 ≤3 步提纲 + native FC 决策），
                    # 消除独立 _plan_step 的额外 LLM 串行往返带来的 P95 增量。
                    plan, actions, content = await self._first_plan_decide(
                        question, history_context, loop, kb_ids, memory_context=memory_context
                    )
                    if plan and not loop.plan_dirty:
                        loop.plan = plan
                        loop.plan_dirty = True
                        yield (
                            "reasoning",
                            self._step_payload(
                                "planned", "Agent 规划",
                                " → ".join(loop.plan),
                                metadata={"plan": list(loop.plan)},
                            ),
                        )
                else:
                    actions, content = await self._decide_step(
                        question, history_context, loop, kb_ids, memory_context=memory_context
                    )
            except Exception as exc:
                log_exception(
                    logger,
                    exc,
                    action="agent.loop.decide",
                    message="Agent 循环决策失败",
                    step=step,
                )
                # L2 V2（2026-09-17）：plan-on 首步 _first_plan_decide 在带 kb_ids 的
                # multi/hybrid 上偶发异常，若直接 break 会把本应进循环的题目吞成
                # `steps=0 / knowledge_base`，静默绕过 Agent。降级用 _decide_step 重决策，
                # 保证 plan-on 也确定性进循环（归并到"强制首步调工具"护栏）。
                if step == 1 and settings.search.AGENT_PLAN_ENABLED:
                    try:
                        actions, content = await self._decide_step(
                            question, history_context, loop, kb_ids, memory_context=memory_context
                        )
                    except Exception as exc2:
                        log_exception(
                            logger,
                            exc2,
                            action="agent.loop.decide_fallback",
                            message="规划降级决策亦失败，结束循环",
                            step=step,
                        )
                        end_reason = "decide_error"
                        break
                else:
                    end_reason = "decide_error"
                    break
            loop.steps = step

            # L2 计划推进：每步成功执行工具后，把剩余计划首项视为已完成（移到 plan_done）
            if loop.plan:
                done_item = loop.plan.pop(0)
                loop.plan_done.append(done_item)

            if not actions:
                # 模型不再调用工具：content 即最终答案（可能为空，交由综合兜底）
                direct_answer = content
                end_reason = "final_answer"
                break

            yield (
                "reasoning",
                self._step_payload(
                    "running", f"Agent 第 {step} 步",
                    f"调用 {', '.join(a['name'] for a in actions)}",
                    metadata={"step_index": step, "tools": [a["name"] for a in actions]},
                ),
            )

            # ---- ACT + OBSERVE ----
            step_start = time.time()
            results = []
            observations = []
            async for ev in self._execute_actions(
                actions, loop, kb_ids=kb_ids, owner_id=owner_id, trace=trace
            ):
                if ev[0] == "action_results":
                    results, observations = ev[1], ev[2]
                else:
                    # 审批事件（approval_required）与审批结果 reasoning 直接透传给 SSE
                    yield ev
            all_results.extend(results)
            for obs in observations:
                loop.scratchpad.append({"step": step, **obs})
            duration_ms = int((time.time() - step_start) * 1000)

            ok_tools = [r.tool_name for r in results if r.success]
            yield (
                "reasoning",
                self._step_payload(
                    "done", f"Agent 第 {step} 步",
                    f"工具{'、'.join(ok_tools) if ok_tools else '全部失败'}，"
                    f"获得 {sum(1 for o in observations if '失败' not in o['observation'] and '重复' not in o['observation'])} 条观察",
                    duration_ms=duration_ms,
                    metadata={"step_index": step, "observations": len(observations)},
                ),
            )

        # ---- 综合 ----
        if collect_only:
            context = self._format_context(all_results)
            sources = self._collect_sources(all_results)
            yield (
                "result",
                {
                    "answer": direct_answer or "",
                    "context": context,
                    "sources": sources,
                    "polluted": False,
                    "steps": loop.steps,
                    "reason": end_reason,
                },
            )
            return

        # 纯 Agent 模式：模型直接给出的答案优先；否则用工具结果综合生成
        if direct_answer:
            # 3B 模型偶发把 scratchpad/工具调用叙述当最终答案输出 → 先清洗再外发
            from .output_sanitizer import OutputSanitizer

            cleaned, polluted = OutputSanitizer.sanitize(direct_answer)
            if cleaned:
                yield ("chunk", cleaned, [], [], answer_type)
            yield (
                "result",
                {"answer": cleaned, "sources": self._collect_sources(all_results),
                 "polluted": polluted, "steps": loop.steps, "reason": end_reason},
            )
            return

        successful = [r for r in all_results if r.success and r.output]
        if not successful:
            yield (
                "result",
                {"answer": "", "sources": [], "polluted": False,
                 "steps": loop.steps, "reason": end_reason},
            )
            return

        sources = self._collect_sources(successful)
        source_texts, source_metadata = self._sources_to_metadata(sources)
        answer_acc = ""
        polluted = False
        if self.answer_generator is not None:
            async for chunk, _, thinking in self.answer_generator.generate_stream(
                question=question,
                history_context=history_context,
                tool_results=successful,
                is_realtime=False,
                think=think,
            ):
                if thinking:
                    yield ("thinking", thinking)
                    continue
                from .output_sanitizer import OutputSanitizer

                cleaned, pol = OutputSanitizer.sanitize(chunk)
                polluted = polluted or pol
                if cleaned:
                    answer_acc += cleaned
                    yield ("chunk", cleaned, source_texts, source_metadata, answer_type)
        yield (
            "result",
            {"answer": answer_acc, "sources": sources, "polluted": polluted,
             "steps": loop.steps, "reason": end_reason},
        )

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _step_payload(status: str, title: str, content: str,
                      duration_ms: Optional[int] = None, metadata: Optional[Dict] = None) -> str:
        from .reasoning import ReasoningStep

        return ReasoningStep(
            step="agent_loop", status=status, title=title, content=content,
            duration_ms=duration_ms, metadata=metadata or {},
        ).to_sse_payload()

    @staticmethod
    def _format_context(results) -> str:
        parts = []
        for i, r in enumerate(results, 1):
            parts.append(f"[工具 {i}] {r.tool_name}\n{r.output}")
        return "\n\n".join(parts)

    def _collect_sources(self, results) -> List[Dict[str, Any]]:
        sources: List[Dict[str, Any]] = []
        for r in results:
            for s in (r.sources or []):
                if not any(s.get("url") and s.get("url") == x.get("url") for x in sources):
                    sources.append(s)
        return sources

    def _sources_to_metadata(self, sources) -> Tuple[List[str], List[Dict[str, Any]]]:
        if self.metadata_converter is not None:
            return self.metadata_converter(sources)
        texts = [s.get("page_content", "") for s in sources]
        return texts, list(sources)
