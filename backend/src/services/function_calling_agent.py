"""Function Calling 搜索 Agent：单轮决策 + 工具执行 + 最终回答（CODE-1 拆分）。"""

import logging
from typing import Any, Dict, List, Tuple

from src.config import settings
from src.services.identity import IDENTITY_PROMPT_RULE
from src.utils.security import redact_for_log

logger = logging.getLogger(__name__)

from src.utils.error_handling import log_exception

from src.services.search_agent_types import (
    SearchAgentResult,
    ToolCall,
    ToolResult,
    looks_like_tool_call,
)
from src.services.search_agent_base import BaseSearchAgent
from src.services.search_agent_types import is_realtime_question

class FunctionCallingHandler(BaseSearchAgent):
    """
    Function Calling 处理器

    流程：
    1. 向 LLM 注册工具（native FC 优先，失败回退 prompt 约定 JSON 模式）
    2. LLM 决定是直接回答还是调用工具
    3. 如需工具，并行执行工具调用
    4. 将工具结果回传给 LLM，生成最终回答
    """

    def _native_tools_schema(self) -> List[Dict[str, Any]]:
        """生成 OpenAI function-calling 风格的工具 schema（native FC 用）。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self.toolkit.tools
        ]

    async def _decide_native(
        self, question: str, history_context: str
    ) -> Tuple[List[ToolCall], str]:
        """原生 function calling 决策（Ollama tools API）。

        Returns:
            (tool_calls, direct_content)：有工具调用时 direct_content 为空；
            无工具调用时 direct_content 为模型直接回答。
        Raises:
            任何异常由调用方捕获并回退 prompt 模式。
        """
        from langchain_core.messages import HumanMessage

        bound = self.llm.bind_tools(self._native_tools_schema())
        prompt = (
            "你是一个智能助手，可以使用工具辅助回答。\n"
            f"{IDENTITY_PROMPT_RULE}\n"
            "若是时效性问题（天气/新闻/价格/汇率/比分等实时数据），必须调用相应工具；\n"
            "稳定知识（概念解释/历史事实）可直接回答，不必调用工具。\n\n"
            f"对话历史：\n{history_context}\n\n用户问题：{question}"
        )
        response = await bound.ainvoke([HumanMessage(content=prompt)])
        tool_calls = [
            ToolCall(name=tc["name"], arguments=tc.get("args") or {})
            for tc in (getattr(response, "tool_calls", None) or [])
            if isinstance(tc, dict) and tc.get("name")
        ]
        direct_content = (response.content or "").strip() if response.content else ""
        return tool_calls, ("" if tool_calls else direct_content)

    async def _decide(
        self, question: str, history_context: str
    ) -> Tuple[List[ToolCall], str]:
        """工具决策：native FC 优先，异常时自动回退 prompt 约定 JSON 模式。"""
        if settings.search.SEARCH_AGENT_NATIVE_FC:
            try:
                tool_calls, content = await self._decide_native(question, history_context)
                logger.info(
                    f"原生 FC 决策成功: tools={[tc.name for tc in tool_calls]}"
                )
                return tool_calls, content
            except Exception as exc:
                log_exception(
                    logger,
                    exc,
                    action="agent.fc.native_decide",
                    message="原生 FC 决策失败，回退 prompt 模式",
                )

        decision_prompt = self._build_decision_prompt(question, history_context)
        try:
            response = await self.llm.ainvoke(decision_prompt)
            content = response.content.strip() if response.content else ""
        except Exception as exc:
            log_exception(
                logger, exc, action="agent.fc.decide", message="Function Calling 决策请求失败"
            )
            content = ""
        return self._parse_tool_calls(content), content

    async def run(self, question: str, history_context: str = "") -> SearchAgentResult:
        tool_calls, content = await self._decide(question, history_context)

        # 模型未调用工具，直接返回答案
        # 但若为强时效性问题，返回空 answer 触发 RAGChain 降级到 Phase 2 直接搜索，
        # 避免 LLM 笼统回复"无法获取实时信息"
        if not tool_calls:
            if is_realtime_question(question):
                logger.info(f"Function Calling 未调工具但识别为时效性问题，触发降级: {redact_for_log(question)}")
                return SearchAgentResult(answer="", context="", sources=[])
            return SearchAgentResult(answer=content, context="", sources=[])

        tool_results = await self._execute_tool_calls(tool_calls)
        sources = self._extract_sources(tool_results)
        context = self._format_tool_results(tool_results)

        final_prompt = self._build_final_prompt(question, history_context, tool_results)
        try:
            final_response = await self.llm.ainvoke(final_prompt)
            answer = final_response.content.strip() if final_response.content else ""
        except Exception as exc:
            log_exception(
                logger, exc, action="agent.fc.final", message="Function Calling 最终生成失败"
            )
            answer = ""

        # 推理模型容易把工具调用 JSON 直接输出为回答。
        # 若检测到这种污染，视为回答无效，返回空 answer 触发 RAGChain 降级到 Phase 2 搜索。
        if looks_like_tool_call(answer):
            logger.warning(f"Function Calling 最终回答被工具 JSON 污染，触发降级: {redact_for_log(answer)}")
            return SearchAgentResult(answer="", context=context, sources=sources, tool_calls=tool_calls)

        return SearchAgentResult(
            answer=answer,
            context=context,
            sources=sources,
            tool_calls=tool_calls,
        )

    async def arun_stream(
        self, question: str, history_context: str = ""
    ):
        """流式版本：工具决策阶段非流式（native FC 优先），最终答案流式输出"""
        tool_calls, content = await self._decide(question, history_context)

        if not tool_calls:
            # 没有工具调用，直接流式返回原内容（统一按 chunk 输出）
            # 但若为强时效性问题，yield 空内容触发 RAGChain 降级到 Phase 2 直接搜索
            if is_realtime_question(question):
                logger.info(f"Function Calling 流式未调工具但识别为时效性问题，触发降级: {redact_for_log(question)}")
                yield "", []
                return
            yield content, []
            return

        tool_results = await self._execute_tool_calls(tool_calls)
        sources = self._extract_sources(tool_results)
        final_prompt = self._build_final_prompt(question, history_context, tool_results)

        try:
            async for chunk in self.llm.astream(final_prompt):
                yield chunk.content if chunk.content else "", sources
        except Exception as exc:
            log_exception(
                logger, exc, action="agent.fc.stream", message="Function Calling 流式生成失败"
            )
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
{IDENTITY_PROMPT_RULE}

可用工具：
{self.toolkit.tools_prompt()}

请按以下规则回复：

【必须使用工具的场景】（以下问题你的训练数据无法覆盖，必须调用 web_search）：
- 天气、气温、预报（如"北京天气""吕梁明天会下雨吗"）
- 新闻、最新消息、热点事件（如"最新AI新闻"）
- 价格、股价、汇率、行情（如"比特币多少钱""今日油价"）
- 实时数据、排名、比分（如"当前空气质量""某赛事比分"）
- 任何包含"最新""最近""今天""现在""当前""实时"等时效性词汇的问题

【不需要工具的场景】：
- 常识、概念解释（如"什么是机器学习"）
- 历史事实（如"二战结束于哪年"）
- 你的训练数据已覆盖的稳定知识

使用工具时，必须严格返回如下格式，且**只返回这一行，不要加 ```json 代码块、不要加 <think> 思考标签、不要加任何解释文字**：
<tool_call>
[{{"name": "web_search", "arguments": {{"query": "搜索关键词", "reason": "搜索原因"}}}}]
</tool_call>

错误示例（不要这样输出）：
- ```json\n[{{"name": "web_search", ...}}]\n```
- <think> 我觉得需要搜索 </think>[{{"name": "web_search", ...}}]

正确示例：
- 用户问"吕梁的天气" → 直接输出：
<tool_call>[{{"name": "web_search", "arguments": {{"query": "吕梁天气", "reason": "查询实时天气"}}}}]</tool_call>
- 用户问"什么是深度学习" → 直接回答，不调用工具

对话历史：
{history_context}

用户问题：{question}

请判断是否需要使用工具（若为时效性问题，务必调用 web_search）："""

    def _build_final_prompt(
        self,
        question: str,
        history_context: str,
        tool_results: List[ToolResult],
    ) -> str:
        results_text = self._format_tool_results(tool_results)
        return f"""你是一个严谨的智能助手，请基于以下工具搜索结果直接回答用户问题。

要求：
1. 只使用工具结果中的信息，禁止编造参考信息里不存在的信息。
2. 如果工具结果中没有答案，直接说明"无法找到相关信息"。
3. 关键事实标注来源编号，如[1]、[2]，对应工具结果中的条目编号。
4. 保持回答简洁、准确、连贯。
5. **禁止输出任何工具调用 JSON、```json 代码块、<tool_call> 标签或 <think> 思考过程**。
6. {IDENTITY_PROMPT_RULE}

对话历史：
{history_context}

用户问题：{question}

工具搜索结果：
{results_text}

请结合搜索结果给出最终回答："""
