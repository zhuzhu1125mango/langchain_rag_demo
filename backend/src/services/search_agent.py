"""
联网搜索 Agent - Phase 3

提供两种与本地大模型深度集成的搜索模式：
1. Function Calling（工具调用）：单轮决策是否需要搜索，执行后生成最终回答
2. ReAct（推理+行动）：多轮迭代搜索，根据观察结果动态调整下一步查询

设计原则：
- 兼容 Ollama 本地模型，不依赖原生 function calling 能力，采用 prompt-based 方式实现
- 全异步：LLM 调用、网页抓取、搜索执行均为异步
- 可降级：模型未按格式输出时，自动回退到直接回答或 Phase 2 搜索上下文
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from src.config import settings

logger = logging.getLogger(__name__)


def _extract_domain(url: str) -> str:
    """从 URL 中提取域名（与 web_search_service.WebSearchService._extract_domain 逻辑一致）"""
    from urllib.parse import urlparse

    try:
        return urlparse(url).netloc.lower().lstrip("www.")
    except Exception:
        return ""


@dataclass
class ToolCall:
    """工具调用描述"""

    name: str
    arguments: Dict[str, Any]


@dataclass
class ToolResult:
    """工具执行结果"""

    tool_call: ToolCall
    result: str
    success: bool = True


@dataclass
class AgentStep:
    """ReAct 单步记录"""

    step: int
    thought: str
    action: Dict[str, Any]
    observation: str


@dataclass
class SearchAgentResult:
    """Agent 搜索结果"""

    answer: str = ""
    context: str = ""
    sources: List[Dict[str, Any]] = field(default_factory=list)
    tool_calls: List[ToolCall] = field(default_factory=list)
    steps: List[AgentStep] = field(default_factory=list)


class Tool:
    """工具定义"""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        handler: Callable[..., Awaitable[str]],
    ):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.handler = handler

    def schema(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


class SearchToolkit:
    """搜索工具集，封装 WebSearchService 为可被 LLM 调用的工具"""

    def __init__(self, web_search_service):
        self.web_search_service = web_search_service
        self._tools = self._build_tools()

    def _build_tools(self) -> List[Tool]:
        return [
            Tool(
                name="web_search",
                description="执行联网搜索，返回相关网页的标题、URL 和摘要，用于获取实时信息或外部知识",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "搜索关键词或问题，建议简洁明确",
                        },
                        "reason": {
                            "type": "string",
                            "description": "进行本次搜索的原因",
                        },
                    },
                    "required": ["query"],
                },
                handler=self._handle_web_search,
            ),
            Tool(
                name="fetch_webpage",
                description="抓取指定网页的完整正文内容，用于深入查看某个搜索结果",
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "目标网页 URL"},
                        "title": {"type": "string", "description": "网页标题（可选）"},
                        "snippet": {"type": "string", "description": "网页摘要（可选）"},
                    },
                    "required": ["url"],
                },
                handler=self._handle_fetch_webpage,
            ),
        ]

    @property
    def tools(self) -> List[Tool]:
        return self._tools

    def get_tool(self, name: str) -> Optional[Tool]:
        for tool in self._tools:
            if tool.name == name:
                return tool
        return None

    def tools_prompt(self) -> str:
        return json.dumps([t.schema() for t in self._tools], ensure_ascii=False, indent=2)

    async def _handle_web_search(self, query: str, **kwargs) -> str:
        if not query:
            return "搜索关键词为空，未执行搜索。"

        try:
            results = await self.web_search_service.search_multi(query)
        except Exception as e:
            logger.warning(f"工具搜索失败 [{query}]: {e}")
            return f"搜索失败: {e}"

        if not results:
            return "未找到相关搜索结果。"

        lines = []
        for i, r in enumerate(results[: settings.search.SEARCH_MAX_RESULTS], 1):
            lines.append(
                f"[{i}] 标题: {r.title}\nURL: {r.url}\n摘要: {r.content[:500]}"
            )
        return "\n\n".join(lines)

    async def _handle_fetch_webpage(self, url: str, title: str = "", snippet: str = "") -> str:
        if not url:
            return "URL 为空，无法抓取。"

        try:
            content = await self.web_search_service.fetch_content(url, title, snippet)
        except Exception as e:
            logger.warning(f"工具抓取失败 [{url}]: {e}")
            return f"抓取失败: {e}"

        if not content:
            return "无法获取网页内容。"

        text = content.content[:3000] if len(content.content) > 3000 else content.content
        return f"标题: {content.title}\nURL: {content.url}\n正文:\n{text}"


class BaseSearchAgent:
    """Agent 基类，提供通用解析与工具执行能力"""

    def __init__(self, llm, toolkit: SearchToolkit):
        self.llm = llm
        self.toolkit = toolkit

    def _extract_json_blocks(self, content: str) -> List[Dict[str, Any]]:
        """从文本中提取 JSON 对象/数组块"""
        results = []

        # 先尝试提取 <tool_call>...</tool_call> 标签
        tag_pattern = r"<tool_call>\s*([\s\S]*?)\s*</tool_call>"
        for match in re.findall(tag_pattern, content):
            match = match.strip()
            if not match:
                continue
            try:
                data = json.loads(match)
                results.extend(self._normalize_json_to_dicts(data))
            except Exception as e:
                logger.debug(f"标签内 JSON 解析失败: {e}")

        if results:
            return results

        # 再尝试整段解析（处理模型直接返回纯 JSON 的情况）
        stripped = content.strip()
        if stripped.startswith(("{", "[")):
            try:
                data = json.loads(stripped)
                return self._normalize_json_to_dicts(data)
            except Exception:
                pass

        # 最后尝试提取独立的 JSON 对象/数组（非贪婪，适用于简单情况）
        json_pattern = r"(\{[\s\S]*?\}|\[[\s\S]*?\])"
        for match in re.findall(json_pattern, content):
            try:
                data = json.loads(match)
                results.extend(self._normalize_json_to_dicts(data))
            except Exception:
                continue

        return results

    def _normalize_json_to_dicts(self, data: Any) -> List[Dict[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            return [data]
        return []

    async def _execute_tool_call(self, tool_call: ToolCall) -> ToolResult:
        tool = self.toolkit.get_tool(tool_call.name)
        if not tool:
            return ToolResult(
                tool_call=tool_call,
                result=f"工具 '{tool_call.name}' 不存在",
                success=False,
            )

        try:
            result = await tool.handler(**tool_call.arguments)
            return ToolResult(tool_call=tool_call, result=result, success=True)
        except Exception as e:
            logger.warning(f"工具执行失败 {tool_call.name}: {e}")
            return ToolResult(tool_call=tool_call, result=f"执行失败: {e}", success=False)

    async def _execute_tool_calls(self, tool_calls: List[ToolCall]) -> List[ToolResult]:
        if not tool_calls:
            return []

        tasks = [self._execute_tool_call(tc) for tc in tool_calls]
        return await asyncio.gather(*tasks)

    def _extract_sources_from_texts(self, texts: List[str]) -> List[Dict[str, Any]]:
        """从文本列表中提取 URL 来源信息（_extract_sources / _extract_sources_from_steps 的公共逻辑）"""
        sources: List[Dict[str, Any]] = []
        url_pattern = re.compile(r"URL:\s*(https?://\S+)", re.IGNORECASE)

        for text in texts:
            # web_search / fetch_webpage 结果中都包含 URL: xxx
            for match in url_pattern.finditer(text):
                url = match.group(1).strip()
                if not any(s.get("url") == url for s in sources):
                    sources.append({
                        "url": url,
                        "source": _extract_domain(url),
                        "document_id": "",
                        "filename": "web_search",
                        "chunk_index": 0,
                        "total_chunks": 1,
                        "page_content": text[:500],
                    })

        return sources

    def _extract_sources(self, tool_results: List[ToolResult]) -> List[Dict[str, Any]]:
        """从工具结果中提取来源信息"""
        texts = [tr.result for tr in tool_results if tr.success]
        return self._extract_sources_from_texts(texts)


class FunctionCallingHandler(BaseSearchAgent):
    """
    Function Calling 处理器

    流程：
    1. 向 LLM 注册 web_search / fetch_webpage 工具
    2. LLM 决定是直接回答还是调用工具
    3. 如需工具，并行执行工具调用
    4. 将工具结果回传给 LLM，生成最终回答
    """

    async def run(self, question: str, history_context: str = "") -> SearchAgentResult:
        decision_prompt = self._build_decision_prompt(question, history_context)
        try:
            response = await self.llm.ainvoke(decision_prompt)
            content = response.content.strip() if response.content else ""
        except Exception as e:
            logger.warning(f"Function Calling 决策请求失败: {e}")
            return SearchAgentResult(answer="")

        tool_calls = self._parse_tool_calls(content)

        # 模型未调用工具，直接返回答案
        if not tool_calls:
            return SearchAgentResult(answer=content, context="", sources=[])

        tool_results = await self._execute_tool_calls(tool_calls)
        sources = self._extract_sources(tool_results)
        context = self._format_tool_results(tool_results)

        final_prompt = self._build_final_prompt(question, history_context, tool_results)
        try:
            final_response = await self.llm.ainvoke(final_prompt)
            answer = final_response.content.strip() if final_response.content else ""
        except Exception as e:
            logger.warning(f"Function Calling 最终生成失败: {e}")
            answer = ""

        return SearchAgentResult(
            answer=answer,
            context=context,
            sources=sources,
            tool_calls=tool_calls,
        )

    async def arun_stream(
        self, question: str, history_context: str = ""
    ):
        """流式版本：工具决策阶段非流式，最终答案流式输出"""
        decision_prompt = self._build_decision_prompt(question, history_context)
        try:
            response = await self.llm.ainvoke(decision_prompt)
            content = response.content.strip() if response.content else ""
        except Exception as e:
            logger.warning(f"Function Calling 决策请求失败: {e}")
            content = ""

        tool_calls = self._parse_tool_calls(content)

        if not tool_calls:
            # 没有工具调用，直接流式返回原内容（统一按 chunk 输出）
            yield content, []
            return

        tool_results = await self._execute_tool_calls(tool_calls)
        sources = self._extract_sources(tool_results)
        final_prompt = self._build_final_prompt(question, history_context, tool_results)

        try:
            async for chunk in self.llm.astream(final_prompt):
                yield chunk.content if chunk.content else "", sources
        except Exception as e:
            logger.warning(f"Function Calling 流式生成失败: {e}")
            yield "", sources

    def _parse_tool_calls(self, content: str) -> List[ToolCall]:
        blocks = self._extract_json_blocks(content)
        calls = []
        for item in blocks:
            name = item.get("name") or item.get("tool")
            arguments = item.get("arguments") or item.get("args") or item.get("params") or {}
            if not isinstance(arguments, dict):
                arguments = {}
            if name:
                calls.append(ToolCall(name=name, arguments=arguments))
        return calls

    def _format_tool_results(self, tool_results: List[ToolResult]) -> str:
        parts = []
        for i, tr in enumerate(tool_results, 1):
            parts.append(f"[工具 {i}] {tr.tool_call.name}({tr.tool_call.arguments})\n{tr.result}")
        return "\n\n".join(parts)

    def _build_decision_prompt(self, question: str, history_context: str) -> str:
        return f"""你是一个智能助手，可以使用工具来辅助回答用户问题。

可用工具：
{self.toolkit.tools_prompt()}

请按以下规则回复：
1. 如果问题需要最新信息、外部知识、实时数据或超出你已有知识范围，请使用工具。
2. 使用工具时，必须严格返回如下 JSON 格式（不要添加额外解释）：
<tool_call>
[{{"name": "web_search", "arguments": {{"query": "搜索关键词", "reason": "搜索原因"}}}}]
</tool_call>
3. 如果不需要工具，直接回答用户问题。

对话历史：
{history_context}

用户问题：{question}

请判断是否需要使用工具："""

    def _build_final_prompt(
        self,
        question: str,
        history_context: str,
        tool_results: List[ToolResult],
    ) -> str:
        results_text = self._format_tool_results(tool_results)
        return f"""你是一个严谨的智能助手，请基于以下工具搜索结果回答用户问题。

要求：
1. 只使用工具结果中的信息，禁止编造参考信息里不存在的信息。
2. 如果工具结果中没有答案，直接说明"无法找到相关信息"。
3. 关键事实标注来源编号，如[1]、[2]，对应工具结果中的条目编号。
4. 保持回答简洁、准确、连贯。

对话历史：
{history_context}

用户问题：{question}

工具搜索结果：
{results_text}

请结合搜索结果给出最终回答："""


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
            except Exception as e:
                logger.warning(f"ReAct 第 {step + 1} 步 LLM 调用失败: {e}")
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
                except Exception as e:
                    observation = f"执行失败: {e}"

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
        except Exception as e:
            logger.warning(f"ReAct 最终综合失败: {e}")
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
            except Exception as e:
                logger.warning(f"ReAct 第 {step + 1} 步 LLM 调用失败: {e}")
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
                except Exception as e:
                    observation = f"执行失败: {e}"

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
