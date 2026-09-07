"""答案生成器。

基于工具执行结果、知识库检索结果和对话历史，调用 LLM 生成自然语言答案。
提供流式和非流式两种接口，输出格式与现有 RAGChain 保持一致。
"""

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from src.services.tools.tool_manager import ToolResult
from src.services.prompts.prompt_loader import PromptLoader
from src.services.context_builder import ContextBuilder
from src.services.numerical_validator import NumericalValidator

logger = logging.getLogger(__name__)


class AnswerGenerator:
    """答案生成器。"""

    def __init__(
        self,
        llm,
        numerical_validator: Optional[NumericalValidator] = None,
        llm_direct=None,
    ):
        self.llm = llm
        # 深度思考关闭时使用的非思考专用模型（think=False 时切换，避免混合模型空烧思考 token）
        self.llm_direct = llm_direct
        self.prompt_loader = PromptLoader()
        self.context_builder = ContextBuilder()
        self.numerical_validator = numerical_validator

    def _select_llm(self, think: Optional[bool]):
        """按深度思考开关选择模型实例。

        True: 思考模型并绑定 reasoning=True；False: 非思考专用模型；
        None: 思考模型默认行为（模型不支持思考时）。
        """
        if think is False and self.llm_direct is not None:
            return self.llm_direct
        if think is not None:
            return self.llm.bind(reasoning=think)
        return self.llm

    def _build_context(
        self,
        tool_results: List[ToolResult],
        kb_docs: List[Any],
        kb_source_metadata: List[Dict[str, Any]],
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """将工具结果和知识库文档整合为上下文文本与来源元数据。

        使用 ContextBuilder 统一处理：去重、预算管理、Lost in the Middle 重排序、统一编号。
        """
        context_text, numbered_sources = self.context_builder.build_context(
            question="",
            kb_docs=kb_docs,
            tool_results=tool_results,
        )

        # 保留工具来源中的置信度等额外信息
        tool_sources = []
        for tr in tool_results or []:
            if getattr(tr, "success", True):
                tool_sources.extend(getattr(tr, "sources", []) or [])

        # 合并 KB 元数据到 numbered_sources（按 source_type 匹配）
        if kb_source_metadata:
            kb_meta_by_content = {
                s.get("content", ""): s for s in kb_source_metadata
            }
            for s in numbered_sources:
                if s.get("source_type") == "kb":
                    meta = kb_meta_by_content.get(s.get("content", ""))
                    if meta:
                        s.update(meta)

        all_sources = list(numbered_sources)
        all_sources.extend(tool_sources)
        return context_text, all_sources

    async def _build_prompt(
        self,
        question: str,
        history_context: str,
        context: str,
        no_context: bool = False,
        is_realtime: bool = False,
    ) -> str:
        """构造生成提示词，优先从模板文件加载。"""
        if no_context:
            if self.prompt_loader.exists("answer_generator_no_context"):
                return await self.prompt_loader.load(
                    "answer_generator_no_context",
                    realtime_note=(
                        "当前问题涉及时效性信息，但你未获得实时数据来源。"
                        if is_realtime
                        else "请基于已有知识回答以下问题。"
                    ),
                    history=history_context,
                    question=question,
                )
            return f"""你是一个智能助手。

{('当前问题涉及时效性信息，但你未获得实时数据来源。' if is_realtime else '请基于已有知识回答以下问题。')}

要求：
1. 非时效性问题：正常回答。
2. 涉及时效性信息（如价格、新闻、天气、实时数据等）：基于已有知识作答后，
   在回答末尾标注"以上信息基于训练数据，可能不具备实时性，建议核实最新情况"。
3. 禁止输出工具调用 JSON、代码块或思考标签。
4. **禁止在回答开头使用"根据搜索结果"、"根据参考信息"、"资料显示"、"我认为"、"经查询"等前缀，直接陈述答案**。

对话历史:
{history_context}

当前问题:
{question}

请结合历史对话进行回答，保持回答的连贯性和上下文一致性。"""

        if self.prompt_loader.exists("answer_generator"):
            return await self.prompt_loader.load(
                "answer_generator",
                history=history_context,
                context=context,
                question=question,
            )

        return f"""你是一个严谨的智能助手，请基于以下参考信息回答用户问题。

回答要求：
1. 优先使用参考信息中的内容，禁止编造参考信息里不存在的信息。
2. 如果参考信息中没有答案，直接说明"无法找到相关信息"。
3. 尽量在事实性陈述后标注来源编号，如[1]、[2]，对应参考信息中的来源编号。
   如果不确定来源编号，可以不标注，系统会自动补全。
4. 如果同时包含工具结果和知识库文档，优先以工具实时结果为准，知识库作为补充。
5. 对于价格、日期、数据等时效性信息，优先提取具体数值并突出展示，标注数据来源。
6. **禁止输出任何工具调用 JSON、```json 代码块、<tool_call> 标签或 <RichMediaReference> 思考过程**。
7. **禁止在回答开头使用"根据搜索结果"、"根据参考信息"、"资料显示"、"我认为"、"经查询"等前缀，直接陈述答案**。

对话历史:
{history_context}

参考信息:
{context}

当前问题:
{question}

请结合参考信息给出准确、连贯的回答："""

    def _append_numerical_warnings(
        self,
        answer: str,
        sources: List[Dict[str, Any]],
    ) -> str:
        """基于多源数值交叉验证追加一致性警告。"""
        if self.numerical_validator is None or len(sources) < 2:
            return answer

        try:
            result = self.numerical_validator.validate(sources)
            if result.conflicts:
                warning = self.numerical_validator.format_warning(result)
                return answer + warning
        except Exception as e:
            logger.warning("数值交叉验证失败，跳过警告追加：%s", e)
        return answer

    async def generate(
        self,
        question: str,
        history_context: str = "",
        tool_results: Optional[List[ToolResult]] = None,
        kb_docs: Optional[List[Any]] = None,
        kb_source_metadata: Optional[List[Dict[str, Any]]] = None,
        is_realtime: bool = False,
        think: Optional[bool] = None,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """非流式生成答案。

        Args:
            think: 深度思考开关（True 强制开启 / False 强制关闭 / None 用模型默认）。
                   仅对支持思考的模型生效（config.OLLAMA_SUPPORTS_THINKING）。

        Returns:
            (answer, sources_metadata)
        """
        tool_results = tool_results or []
        kb_docs = kb_docs or []
        kb_source_metadata = kb_source_metadata or []

        context, sources = self._build_context(tool_results, kb_docs, kb_source_metadata)
        if not context:
            prompt = await self._build_prompt(
                question, history_context, "", no_context=True, is_realtime=is_realtime
            )
        else:
            prompt = await self._build_prompt(question, history_context, context)

        llm = self._select_llm(think)
        answer = await llm.ainvoke(prompt)
        answer.content = self._append_numerical_warnings(answer.content, sources)
        return answer.content, sources

    async def generate_stream(
        self,
        question: str,
        history_context: str = "",
        tool_results: Optional[List[ToolResult]] = None,
        kb_docs: Optional[List[Any]] = None,
        kb_source_metadata: Optional[List[Dict[str, Any]]] = None,
        is_realtime: bool = False,
        think: Optional[bool] = None,
    ):
        """流式生成答案。

        Args:
            think: 深度思考开关（True 强制开启 / False 强制关闭 / None 用模型默认）。

        Yields:
            (chunk_content, sources_metadata, thinking_content)
            thinking_content 为模型原始思考文本增量（reasoning=True 时出现在
            additional_kwargs["reasoning_content"]），无思考时为空字符串。
        """
        tool_results = tool_results or []
        kb_docs = kb_docs or []
        kb_source_metadata = kb_source_metadata or []

        context, sources = self._build_context(tool_results, kb_docs, kb_source_metadata)
        if not context:
            prompt = await self._build_prompt(
                question, history_context, "", no_context=True, is_realtime=is_realtime
            )
        else:
            prompt = await self._build_prompt(question, history_context, context)

        llm = self._select_llm(think)
        async for chunk in llm.astream(prompt):
            thinking = (chunk.additional_kwargs or {}).get("reasoning_content") or ""
            yield chunk.content, sources, thinking

    async def generate_with_citation(
        self,
        question: str,
        history_context: str = "",
        tool_results: Optional[List[ToolResult]] = None,
        kb_docs: Optional[List[Any]] = None,
        kb_source_metadata: Optional[List[Dict[str, Any]]] = None,
        search_sources: Optional[List[Dict[str, Any]]] = None,
        is_realtime: bool = False,
        citation_backfiller: Optional[Any] = None,
        think: Optional[bool] = None,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """生成答案并自动补全内联引用。

        在 generate() 基础上，调用 CitationBackfiller 对生成答案进行引用补全：
        - 已有 [n] → 校验有效性，无效改 [?]。
        - 无引用 → embedding 匹配补 [n]，无法匹配补 [?]。

        Args:
            question: 用户问题。
            history_context: 对话历史上下文文本。
            tool_results: 工具执行结果列表。
            kb_docs: 知识库检索文档列表。
            kb_source_metadata: 知识库来源元数据。
            search_sources: 搜索来源列表，形如
                            [{"source_index": 1, "title": ..., "content": ..., "url": ...}]，
                            供 CitationBackfiller 匹配使用。为 None 时不做 embedding 补全。
            is_realtime: 是否为时效性问题。
            citation_backfiller: 注入的 CitationBackfiller 实例。
                                 为 None 时跳过引用补全，直接返回原始答案。

        Returns:
            (answer_with_citations, sources)：补全引用后的答案与来源元数据。
        """
        answer, sources = await self.generate(
            question=question,
            history_context=history_context,
            tool_results=tool_results,
            kb_docs=kb_docs,
            kb_source_metadata=kb_source_metadata,
            is_realtime=is_realtime,
            think=think,
        )

        # 无 backfiller 或无搜索来源 → 直接返回（仅依赖 LLM 自身标注）
        if citation_backfiller is None or not search_sources:
            return answer, sources

        # 合并搜索来源与已有 sources，供 backfiller 匹配和数值验证
        backfill_sources = list(search_sources)
        try:
            answer = await citation_backfiller.backfill(answer, backfill_sources)
        except Exception as e:
            # 引用补全失败不应阻塞主流程，保留原始答案
            logger.warning("引用补全失败，保留原始答案：%s", e)

        # 基于全部来源（KB + 工具 + 搜索）进行数值交叉验证
        all_sources = list(sources)
        for src in search_sources or []:
            if src not in all_sources:
                all_sources.append(src)
        answer = self._append_numerical_warnings(answer, all_sources)

        return answer, sources
