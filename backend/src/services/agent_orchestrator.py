"""有界 Agent 循环编排器（Agent 演进 Phase 2，见 docs/design/agent-evolution.md §4）。

替代 rag_chain 中 FunctionCalling/ReAct 两套并存的 Agent 分支：
- DECIDE：bind_tools(AGENT_TOOLS_ENABLED 白名单) + think=False，输出"调用工具"或"最终答案"
- ACT：ToolManager 并行执行
- OBSERVE：观察截断 + 同参数去重，追加 scratchpad
- 终止：模型给出最终答案 / 步数上限（SEARCH_REACT_MAX_STEPS）/ 时间预算（AGENT_TIME_BUDGET_MS）
- 综合：纯 Agent 模式用 AnswerGenerator 流式生成；混合模式仅收集上下文交给现有生成阶段

事件协议（与 _pipeline 元组协议兼容）：
- ("reasoning", payload)  循环步骤事件（透传给 SSE）
- ("thinking", text)      综合阶段思考增量（透传）
- ("chunk", text, source_texts, source_metadata, answer_type)  综合阶段答案片段（仅纯 Agent 模式）
- ("result", payload_dict) 终态：{answer, sources, polluted, tool_results, steps, reason}

失败出口：零工具调用且答案为空/被污染 → result.answer 为空，由 rag_chain
沿用既有降级链（Phase 2 搜索 → LLM 直答）。
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from src.config import settings

logger = logging.getLogger("rag_system")


@dataclass
class AgentLoopState:
    """单次请求的循环状态（不跨请求复用）。"""

    scratchpad: List[Dict[str, Any]] = field(default_factory=list)
    seen_calls: set = field(default_factory=set)   # 同参数调用去重
    steps: int = 0
    deadline: float = 0.0
    plan: List[str] = field(default_factory=list)   # L2 分层规划：剩余待执行提纲（步骤描述）
    plan_done: List[str] = field(default_factory=list)  # 已完成规划步骤（推动计划修订）
    plan_dirty: bool = False                        # plan 是否已产出（首步规划后置 True）


class AgentOrchestrator:
    """有界 plan-execute 循环（默认经 AGENT_ORCHESTRATOR_ENABLED 灰度开启）。"""

    def __init__(
        self,
        llm,
        tool_manager,
        answer_generator=None,
        metadata_converter=None,
        max_steps: Optional[int] = None,
    ):
        """
        Args:
            llm: 决策用 LLM（应已绑定 think=False，辅助任务不烧思考链）。
            tool_manager: 全局 ToolManager（工具注册与执行入口）。
            answer_generator: AnswerGenerator（纯 Agent 模式综合生成）。
            metadata_converter: 来源 → 管线统一 metadata 的转换函数
                                （rag_chain._web_sources_to_metadata），可选。
            max_steps: 步数上限，缺省取 SEARCH_REACT_MAX_STEPS。
        """
        self.llm = llm
        self.tool_manager = tool_manager
        self.answer_generator = answer_generator
        self.metadata_converter = metadata_converter
        self.max_steps = max_steps or settings.search.SEARCH_REACT_MAX_STEPS

    # ------------------------------------------------------------------
    # 工具面
    # ------------------------------------------------------------------
    def _enabled_tools(self):
        """按 AGENT_TOOLS_ENABLED 白名单解析工具实例（缺省跳过未注册项）。"""
        whitelist = [
            n.strip()
            for n in settings.search.AGENT_TOOLS_ENABLED.split(",")
            if n.strip()
        ]
        tools = []
        for name in whitelist:
            tool = self.tool_manager.registry.get(name)
            if tool is None:
                logger.debug(f"Agent 工具白名单项未注册，跳过: {name}")
                continue
            tools.append(tool)
        return tools

    def _tools_schema(self) -> List[Dict[str, Any]]:
        return [tool.schema() for tool in self._enabled_tools()]

    # ------------------------------------------------------------------
    # 决策
    # ------------------------------------------------------------------
    @staticmethod
    def _consecutive_failed_steps(loop: AgentLoopState) -> int:
        """从 scratchpad 尾部统计连续『无有效结果』的步数（自省触发依据）。

        单步视为无有效结果：观察含 '执行失败'/'重复调用'，或观察文本为空。
        用于在连续失败时引导模型换策略或直接基于自身知识回答，避免空循环。
        """
        n = 0
        for entry in reversed(loop.scratchpad):
            obs = entry.get("observation") or ""
            if (
                "执行失败" in obs
                or "重复调用" in obs
                or not obs.strip()
            ):
                n += 1
            else:
                break
        return n

    def _build_decide_prompt(
        self, question: str, history_context: str, loop: AgentLoopState,
        kb_ids: Optional[List[str]] = None,
        memory_context: str = "",
    ) -> str:
        scratch_lines = []
        for entry in loop.scratchpad:
            obs = entry["observation"]
            scratch_lines.append(
                f"第 {entry['step']} 步：调用 {entry['tool']}({json.dumps(entry['args'], ensure_ascii=False)})\n"
                f"观察结果：{obs}"
            )
        scratch_text = "\n\n".join(scratch_lines) if scratch_lines else "（尚未执行任何工具）"
        kb_guidance = (
            "5. 用户已绑定知识库，与知识库主题相关的问题请优先调用 kb_search 检索知识库，"
            "知识库能覆盖的问题不要依赖 web_search。\n"
            if kb_ids
            else ""
        )
        failed_steps = self._consecutive_failed_steps(loop)
        self_reflect = (
            ""
            if failed_steps < 2
            else (
                "注意：你已连续 "
                + str(failed_steps)
                + " 步未能获得有效结果。若继续尝试工具仍无把握，"
                "请直接基于自身知识给出当前问题的最佳答案，"
                "并在回答末尾标注『知识库未覆盖，以下基于我方常识回答』。\n"
            )
        )
        memory_block = ""
        if memory_context:
            memory_block = (
                "用户此前对话的历史记忆（供参考，按时间由近到远）：\n"
                f"{memory_context}\n\n"
            )
        plan_block = ""
        if loop.plan_dirty:
            planned = "、".join(f"[{i}]{t}" for i, t in enumerate(loop.plan, 1))
            done = list(loop.plan_done) if loop.plan_done else []
            plan_block = (
                f"执行计划（剩余）：{planned or '（无，可综合）'}\n"
                f"已完成计划：{'、'.join(done) if done else '（尚未）'}\n"
                "请按剩余计划推进，已完成的步骤不要重复。\n\n"
            )
        return (
            "你是一个严谨的智能助手，可以调用工具来回答用户问题。\n"
            "规则：\n"
            "1. 每次只做一步决策：要么调用一个工具，要么给出最终答案。\n"
            "2. 已有观察结果足够回答时，立即给出最终答案，不要重复调用工具。\n"
            "3. 工具失败或结果为空时，换一个工具或换个查询词，不要原样重试。\n"
            "4. 最终答案必须整合已有观察结果，标注来源编号（如[1]）。\n"
            "5. 严谨作答：只断言已被检索/观察直接支持的事实；来源缺失、不足或相互冲突时，"
            "明确说明『无法从已有资料确认』，不要自行补全或推断成肯定结论。\n"
            f"{self_reflect}"
            f"{kb_guidance}"
            f"{memory_block}"
            f"{plan_block}"
            f"对话历史：\n{history_context}\n\n"
            f"用户问题：{question}\n\n"
            f"已执行的步骤与观察：\n{scratch_text}\n\n"
            "请做出下一步决策（调用工具，或直接输出最终答案）："
        )

    # ------------------------------------------------------------------
    # 决策公共助手
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_tool_calls(response) -> List[Dict[str, Any]]:
        """从 LLM 响应提取原生 function-calling 决策。"""
        return [
            {"name": tc["name"], "args": tc.get("args") or {}}
            for tc in (getattr(response, "tool_calls", None) or [])
            if isinstance(tc, dict) and tc.get("name")
        ]

    FORCE_FIRST_TOOL_SUFFIX = (
        "【强制要求】你当前尚未调用任何工具，而该问题需要先检索资料才能可靠作答。"
        "本步必须调用工具（知识库优先 kb_search，否则 web_search）。"
        "禁止此刻给出最终答案。"
    )

    # ------------------------------------------------------------------
    # L2 分层规划
    # ------------------------------------------------------------------
    @staticmethod
    def _plan_prompt(question: str, history_context: str) -> str:
        """产步骤提纲的提示词（≤3 步，供单次文本决策）。"""
        return (
            "你是任务规划器。请把下列问题拆解为最多 3 个可调用工具完成的步骤提纲。\n"
            "只输出每行一个步骤的简短目标（不要编号外的解释，不要 JSON）：\n"
            "步骤1：...\n步骤2：...\n步骤3：...\n"
            "若问题无需拆解，仅输出：无需拆解。\n\n"
            f"对话历史：{history_context or '（无）'}\n\n"
            f"问题：{question}"
        )

    async def _plan_step(
        self, question: str, history_context: str, loop: AgentLoopState,
    ) -> List[str]:
        """首步产出 ≤3 步提纲写入 loop.plan（think=False 文本决策）。

        解析每行『步骤N：目标』；解析不到/显式『无需拆解』时置空 plan
        （回退为逐工具反应模式，保证普通问题不引入额外规划延迟不劣化 P95）。
        """
        if not settings.search.AGENT_PLAN_ENABLED:
            return []
        from langchain_core.messages import HumanMessage

        try:
            response = await self.llm.ainvoke(
                [HumanMessage(content=self._plan_prompt(question, history_context))]
            )
            text = (response.content or "").strip() if response.content else ""
        except Exception as e:
            logger.warning(f"Agent 规划失败，回退逐工具反应: {e}")
            return []
        return self._parse_plan_lines(text)

    @staticmethod
    def _parse_plan_lines(text: str) -> List[str]:
        """从文本中解析『步骤N：目标』提纲（≤3 步；『无需拆解』/空行忽略）。"""
        plans = []
        for line in (text or "").splitlines():
            line = line.strip()
            if not line.startswith("步骤") or "：" not in line:
                continue
            target = line.split("：", 1)[1].strip()
            if target and target not in ("无需拆解",):
                plans.append(target)
        return plans[:3]

    @staticmethod
    def _strip_plan_lines(text: str) -> str:
        """去掉 body 中的计划行/『无需拆解』，还原纯最终答案。"""
        out = []
        for line in (text or "").splitlines():
            s = line.strip()
            if (s.startswith("步骤") and "：" in s) or s == "无需拆解":
                continue
            out.append(line)
        return "\n".join(out).strip()

    async def _first_plan_decide(
        self, question: str, history_context: str, loop: AgentLoopState,
        kb_ids: Optional[List[str]] = None, memory_context: str = "",
    ) -> Tuple[List[str], List[Dict[str, Any]], str]:
        """首步：单次 LLM 调用（native FC）产出 规划 + 第一步决策。

        融合 _plan_step（规划）与 _decide_step（决策）为一次往返，消除 L2 规划在本地
        单吞吐 LLM 下的额外串行调用（约 8s，P95 超标根因）；决策仍走原生 function
        calling 保证工具调用可靠性。规划从正文/推理文本解析，解析不到则回退为空 plan。
        """
        from langchain_core.messages import HumanMessage

        bound = self.llm.bind_tools(self._tools_schema())
        plan_instr = (
            "\n\n在回复正文中先给出你的执行计划（≤3 步，每行『步骤N：目标』；"
            "无需拆解则输出『无需拆解』），再调用工具或给出最终答案。"
        )
        base_prompt = self._build_decide_prompt(
            question, history_context, loop, kb_ids, memory_context
        ) + plan_instr
        response = await bound.ainvoke([HumanMessage(content=base_prompt)])
        tool_calls = self._extract_tool_calls(response)
        content = (response.content or "").strip() if response.content else ""
        reasoning = (
            (response.additional_kwargs or {}).get("reasoning")
            if hasattr(response, "additional_kwargs") else None
        ) or ""
        plan = self._parse_plan_lines(content) or self._parse_plan_lines(str(reasoning))
        if tool_calls:
            return plan, tool_calls, ""
        # 强制首步调工具护栏（L2 对拍）：首步且无任何观察时禁止直接给最终答案，
        # 兜底重试一次强制先调工具；仍不调工具则回退直接答案，避免死循环。
        if not loop.scratchpad:
            retry = await bound.ainvoke(
                [HumanMessage(content=base_prompt + "\n\n" + self.FORCE_FIRST_TOOL_SUFFIX)]
            )
            tool_calls = self._extract_tool_calls(retry)
            if tool_calls:
                return plan, tool_calls, ""
        final = self._strip_plan_lines(content) if (plan or "无需拆解" in content) else content
        return plan, [], final

    async def _decide_step(
        self, question: str, history_context: str, loop: AgentLoopState,
        kb_ids: Optional[List[str]] = None,
        memory_context: str = "",
    ) -> Tuple[List[Dict[str, Any]], str]:
        """单步决策（native FC）。返回 (tool_calls, direct_content)。"""
        from langchain_core.messages import HumanMessage

        bound = self.llm.bind_tools(self._tools_schema())
        base_prompt = self._build_decide_prompt(question, history_context, loop, kb_ids, memory_context)
        response = await bound.ainvoke(
            [HumanMessage(content=base_prompt)]
        )
        tool_calls = self._extract_tool_calls(response)
        if tool_calls:
            return tool_calls, ""
        # 强制首步调工具护栏（L2 对拍）：首步且无任何观察时禁止直接给最终答案，
        # 兜底重试一次强制先调工具；仍不调工具则回退直接答案，避免死循环。
        if not loop.scratchpad:
            retry = await bound.ainvoke(
                [HumanMessage(content=base_prompt + "\n\n" + self.FORCE_FIRST_TOOL_SUFFIX)]
            )
            tool_calls = self._extract_tool_calls(retry)
            if tool_calls:
                return tool_calls, ""
        content = (response.content or "").strip() if response.content else ""
        return [], content

    # ------------------------------------------------------------------
    # 执行与观察
    # ------------------------------------------------------------------
    @staticmethod
    def _call_key(name: str, args: Dict[str, Any]) -> str:
        try:
            return f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
        except (TypeError, ValueError):
            return f"{name}:{str(args)}"

    async def _execute_actions(
        self, actions: List[Dict[str, Any]], loop: AgentLoopState,
        kb_ids: Optional[List[str]] = None,
    ):
        """执行本步全部工具调用（去重后并行），返回 (results, observations)。

        kb_search / wiki_lookup 由模型发起时模型无法得知会话 KB ID，
        此处统一注入当前会话 kb_ids（工具侧仍允许显式传参覆盖）。
        """
        from src.services.tools.tool_manager import ToolResult

        pending = []
        observations: List[Dict[str, str]] = []
        for action in actions:
            name = action["name"]
            args = action.get("args") or {}
            if name in ("kb_search", "wiki_lookup") and kb_ids and not args.get("kb_ids"):
                args = {**args, "kb_ids": list(kb_ids)}
            key = self._call_key(name, args)
            if key in loop.seen_calls:
                observations.append({
                    "tool": name,
                    "args": args,
                    "observation": "重复调用（相同参数已执行过）。请更换查询词或直接总结已有结果作答。",
                })
                continue
            loop.seen_calls.add(key)
            pending.append({"tool_name": name, "arguments": args, "key": key})

        results: List[ToolResult] = []
        if pending:
            calls = [{"tool_name": p["tool_name"], "arguments": p["arguments"]} for p in pending]
            results = await self.tool_manager.execute_parallel(calls)

        max_chars = settings.search.AGENT_OBS_MAX_CHARS
        for p, result in zip(pending, results):
            output = result.output if result.success else f"执行失败: {result.error}"
            if len(output) > max_chars:
                output = output[:max_chars] + "…(已截断)"
            observations.append({
                "tool": p["tool_name"],
                "args": p["arguments"],
                "observation": output,
            })
            # ToolResult 携带 key 便于综合阶段排除去重占位
            result.input_arguments = p["arguments"]
        return results, observations

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    async def run_stream(
        self,
        question: str,
        history_context: str = "",
        kb_ids: Optional[List[str]] = None,
        collect_only: bool = False,
        think: bool = False,
        answer_type: str = "agent_orchestrator",
        memory_context: str = "",
    ) -> AsyncIterator[Tuple]:
        """有界循环 + 综合。

        Args:
            question: 当前问题（已消解）。
            collect_only: True = 混合模式（仅收集上下文，交给现有生成阶段）；
                          False = 纯 Agent 模式（循环结束后流式综合输出）。
            think: 综合阶段是否启用深度思考（deep_thinking 开关透传）。
            memory_context: 跨请求记忆（L1-a）。由外部（rag_chain）检索当前会话的历史
                          摘要注入，附加到决策 prompt，实现会话级记忆。
        """
        # perf_counter：单调时钟，避免 time.time() 在 Windows 上的粗粒度/回退问题
        loop = AgentLoopState(
            deadline=time.perf_counter() + settings.search.AGENT_TIME_BUDGET_MS / 1000.0
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
            except Exception as e:
                logger.warning(f"Agent 循环第 {step} 步决策失败: {e}")
                # L2 V2（2026-09-17）：plan-on 首步 _first_plan_decide 在带 kb_ids 的
                # multi/hybrid 上偶发异常，若直接 break 会把本应进循环的题目吞成
                # `steps=0 / knowledge_base`，静默绕过 Agent。降级用 _decide_step 重决策，
                # 保证 plan-on 也确定性进循环（归并到"强制首步调工具"护栏）。
                if step == 1 and settings.search.AGENT_PLAN_ENABLED:
                    try:
                        actions, content = await self._decide_step(
                            question, history_context, loop, kb_ids, memory_context=memory_context
                        )
                    except Exception as e2:
                        logger.warning(f"规划降级决策亦失败，结束循环: {e2}")
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
            results, observations = await self._execute_actions(actions, loop, kb_ids=kb_ids)
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
            yield ("chunk", direct_answer, [], [], answer_type)
            yield (
                "result",
                {"answer": direct_answer, "sources": self._collect_sources(all_results),
                 "polluted": False, "steps": loop.steps, "reason": end_reason},
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
