"""Agent 规划与决策：plan 解析、首步规划、逐步决策（CODE-1 拆分）。"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from src.config import settings

logger = logging.getLogger("rag_system")

from src.utils.error_handling import log_exception

from src.services.agent_state import AgentLoopState

class AgentPlannerMixin:
    """规划与决策，由 AgentOrchestrator 组合。"""

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
    FORCE_FIRST_TOOL_SUFFIX_HYBRID = (
        "【强制要求】你当前尚未调用任何工具，而本次为**知识库+联网混合问题**。"
        "本步必须调用工具，且最终需**同时**用到 kb_search（公司/内部资料）与 "
        "web_search（行业/法定/外部标准）两源；可本步一并调用，也可先 kb 后 web。"
        "禁止此刻给出最终答案。"
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
        except Exception as exc:
            # 规划失败即回退逐工具反应；瞬时故障记 warning，编程错误升级为 error
            log_exception(
                logger, exc, action="agent.plan", message="Agent 规划失败，回退逐工具反应"
            )
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
                [HumanMessage(content=base_prompt + "\n\n" + self._force_first_suffix(loop))]
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
                [HumanMessage(content=base_prompt + "\n\n" + self._force_first_suffix(loop))]
            )
            tool_calls = self._extract_tool_calls(retry)
            if tool_calls:
                return tool_calls, ""
        content = (response.content or "").strip() if response.content else ""
        return [], content

    # ------------------------------------------------------------------
    # 执行与观察
    # ------------------------------------------------------------------
