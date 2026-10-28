"""ReAct 搜索 Agent：多轮推理-行动循环与流式输出（CODE-1 拆分）。"""

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from src.config import settings

logger = logging.getLogger(__name__)

from src.utils.error_handling import log_exception

from src.services.search_agent_types import AgentStep, SearchAgentResult
from src.services.search_agent_base import BaseSearchAgent
from src.services.search_toolkit import SearchToolkit

class ReActAgent(BaseSearchAgent):
    """
    ReAct 多步推理搜索 Agent

    流程：
    1. 维护 Thought / Action / Observation 循环
    2. 每步 LLM 决定是继续搜索还是给出最终答案
    3. 支持多 Query 并行探索，自动根据观察调整下一步
    4. 达到最大步数或 LLM 给出最终答案时停止
    """

    def __init__(self, llm, toolkit: SearchToolkit, max_steps: Optional[int] = None):
        super().__init__(llm, toolkit)
        self.max_steps = max_steps or settings.search.SEARCH_REACT_MAX_STEPS

    async def run(self, question: str, history_context: str = "") -> SearchAgentResult:
        state = {
            "question": question,
            "history_context": history_context,
            "steps": [],
            "search_history": [],
        }

        for step in range(self.max_steps):
            prompt = self._build_react_prompt(state)
            try:
                response = await self.llm.ainvoke(prompt)
                content = response.content.strip() if response.content else ""
            except Exception as exc:
                log_exception(
                    logger,
                    exc,
                    action="agent.react.step_llm",
                    message="ReAct 步内 LLM 调用失败",
                    step=step + 1,
                )
                break

            thought, action_name, action_args, final_answer = self._parse_react(content)

            if final_answer is not None:
                return SearchAgentResult(
                    answer=final_answer,
                    context=self._format_steps(state["steps"]),
                    sources=self._extract_sources_from_steps(state["steps"]),
                    steps=state["steps"],
                )

            if not action_name:
                # 模型没有给出行动，也没有给出最终答案，视为直接回答
                return SearchAgentResult(
                    answer=content,
                    context=self._format_steps(state["steps"]),
                    sources=self._extract_sources_from_steps(state["steps"]),
                    steps=state["steps"],
                )

            tool = self.toolkit.get_tool(action_name)
            if not tool:
                observation = f"工具 '{action_name}' 不存在，可用工具: web_search, fetch_webpage"
            else:
                try:
                    observation = await tool.handler(**action_args)
                except Exception as exc:
                    log_exception(
                        logger,
                        exc,
                        action="agent.react.tool",
                        message="ReAct 工具执行失败",
                        tool=action_name,
                    )
                    observation = f"执行失败: {exc}"

            state["steps"].append(
                AgentStep(
                    step=step + 1,
                    thought=thought or "",
                    action={"name": action_name, "args": action_args},
                    observation=observation,
                )
            )

            if action_name == "web_search" and action_args.get("query"):
                state["search_history"].append(action_args["query"])

        # 达到最大步数，进行最终综合
        final_prompt = self._build_final_prompt(state)
        try:
            response = await self.llm.ainvoke(final_prompt)
            answer = response.content.strip() if response.content else ""
        except Exception as exc:
            log_exception(logger, exc, action="agent.react.final", message="ReAct 最终综合失败")
            answer = ""

        return SearchAgentResult(
            answer=answer,
            context=self._format_steps(state["steps"]),
            sources=self._extract_sources_from_steps(state["steps"]),
            steps=state["steps"],
        )

    async def arun_stream(self, question: str, history_context: str = ""):
        """流式版本：多步搜索完成后，最终答案流式输出"""
        state = {
            "question": question,
            "history_context": history_context,
            "steps": [],
            "search_history": [],
        }

        for step in range(self.max_steps):
            prompt = self._build_react_prompt(state)
            try:
                response = await self.llm.ainvoke(prompt)
                content = response.content.strip() if response.content else ""
            except Exception as exc:
                log_exception(
                    logger,
                    exc,
                    action="agent.react.stream_step_llm",
                    message="ReAct 流式步内 LLM 调用失败",
                    step=step + 1,
                )
                break

            thought, action_name, action_args, final_answer = self._parse_react(content)

            if final_answer is not None:
                sources = self._extract_sources_from_steps(state["steps"])
                yield final_answer, sources
                return

            if not action_name:
                sources = self._extract_sources_from_steps(state["steps"])
                yield content, sources
                return

            tool = self.toolkit.get_tool(action_name)
            if not tool:
                observation = f"工具 '{action_name}' 不存在"
            else:
                try:
                    observation = await tool.handler(**action_args)
                except Exception as exc:
                    log_exception(
                        logger,
                        exc,
                        action="agent.react.stream_tool",
                        message="ReAct 流式工具执行失败",
                        tool=action_name,
                    )
                    observation = f"执行失败: {exc}"

            state["steps"].append(
                AgentStep(
                    step=step + 1,
                    thought=thought or "",
                    action={"name": action_name, "args": action_args},
                    observation=observation,
                )
            )
            if action_name == "web_search" and action_args.get("query"):
                state["search_history"].append(action_args["query"])

        final_prompt = self._build_final_prompt(state)
        sources = self._extract_sources_from_steps(state["steps"])
        try:
            async for chunk in self.llm.astream(final_prompt):
                yield chunk.content if chunk.content else "", sources
        except Exception as e:
            logger.warning(f"ReAct 流式生成失败: {e}")
            yield "", sources

    def _parse_react(self, content: str) -> Tuple[str, Optional[str], Dict[str, Any], Optional[str]]:
        """解析 ReAct 输出：思考、行动、最终答案"""
        thought = ""
        action_name = None
        action_args: Dict[str, Any] = {}
        final_answer = None

        # 思考
        thought_match = re.search(
            r"(?:思考|Thought)[：:]\s*(.*?)(?=\n(?:行动|Action|回答|Answer)[：:]|$)",
            content,
            re.DOTALL | re.IGNORECASE,
        )
        if thought_match:
            thought = thought_match.group(1).strip()

        # 最终答案
        answer_match = re.search(
            r"(?:回答|Answer)[：:]\s*(.*)", content, re.DOTALL | re.IGNORECASE
        )
        if answer_match:
            final_answer = answer_match.group(1).strip() or None

        # 行动（JSON 格式）
        action_match = re.search(
            r"(?:行动|Action)[：:]\s*(\{[\s\S]*)", content, re.DOTALL | re.IGNORECASE
        )
        if action_match:
            action_text = action_match.group(1).strip()
            # 使用平衡大括号提取完整 JSON 对象（支持嵌套参数）
            balanced = self._extract_balanced_json(action_text)
            if balanced:
                try:
                    data = json.loads(balanced)
                    action_name = data.get("name") or data.get("tool")
                    action_args = data.get("arguments") or data.get("args") or data.get("params") or {}
                    if not isinstance(action_args, dict):
                        action_args = {}
                except Exception:
                    logger.debug(f"ReAct 行动 JSON 解析失败: {balanced[:200]}")

        return thought, action_name, action_args, final_answer

    def _extract_balanced_json(self, text: str) -> Optional[str]:
        """从文本开头提取完整的大括号 JSON 对象（支持嵌套）"""
        start = text.find("{")
        if start == -1:
            return None

        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue

            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]

        return None

    def _format_steps(self, steps: List[AgentStep]) -> str:
        parts = []
        for s in steps:
            parts.append(
                f"步骤 {s.step}:\n思考: {s.thought}\n行动: {s.action['name']}({s.action['args']})\n观察: {s.observation}"
            )
        return "\n\n".join(parts)

    def _extract_sources_from_steps(self, steps: List[AgentStep]) -> List[Dict[str, Any]]:
        return self._extract_sources_from_texts([s.observation for s in steps])

    def _build_react_prompt(self, state: Dict[str, Any]) -> str:
        steps_text = self._format_steps(state["steps"])
        search_history = state.get("search_history", [])
        history_hint = ""
        if search_history:
            history_hint = f"已尝试搜索: {', '.join(search_history)}，请避免重复相同查询。"

        return f"""你正在使用 ReAct（思考-行动-观察）方式回答用户问题。你可以使用以下工具：

可用工具：
{self.toolkit.tools_prompt()}

请严格按以下格式回复：

思考：分析当前问题，判断是否已经掌握足够信息，或者需要进一步搜索
行动：{{"name": "工具名", "arguments": {{参数}}}}  （如果需要更多信息）
回答：最终答案  （如果信息已足够）

要求：
- 每次只输出一个"思考"和一个"行动"或"回答"
- 如果信息不足，优先使用 web_search 工具进行搜索
- 如需深入了解某个结果，使用 fetch_webpage 工具
- 不要编造工具结果中没有的信息
- 避免重复之前已经搜索过的相同关键词
{history_hint}

对话历史：
{state['history_context']}

用户问题：
{state['question']}

已执行步骤：
{steps_text}

现在请继续："""

    def _build_final_prompt(self, state: Dict[str, Any]) -> str:
        steps_text = self._format_steps(state["steps"])
        return f"""请基于以下 ReAct 多步搜索过程，给出用户问题的最终答案。

要求：
1. 只使用搜索过程中观察到的信息。
2. 如果信息不足，说明"无法找到相关信息"。
3. 关键事实标注来源编号，对应步骤编号。
4. 保持回答简洁、准确、连贯。

对话历史：
{state['history_context']}

用户问题：
{state['question']}

搜索过程：
{steps_text}

请给出最终答案："""
