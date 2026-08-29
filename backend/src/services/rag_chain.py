"""RAG 问答链 - 检索增强生成核心模块。

负责问题类型判断、知识库检索、上下文构建与 LLM 流式生成回答。
将上下文增强、问题处理、知识库对比等职责委托给独立服务模块。
"""

import time
import asyncio
import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any
from langchain_ollama import ChatOllama

logger = logging.getLogger("rag_system")
from src.config import settings
from .strategy_manager import create_default_strategy_manager
from .decision_pipeline import DecisionPipeline, QAMode, DecisionResult
from .web_search_service import WebSearchService
from .search_agent import SearchToolkit, FunctionCallingHandler, ReActAgent, SearchAgentResult
from .context_enhancer import ContextEnhancer
from .question_processor import QuestionProcessor
from .kb_comparator import KBComparator
from .document_analyzer import DocumentAnalyzer
from .kb_recommender import KBRecommender
from .knowledge_graph_generator import KnowledgeGraphGenerator
from .tools.datetime_tool import build_datetime_answer
from .intent_router import IntentRouter, PrimaryMode, FallbackStrategy
from .tool_executor import ToolExecutor
from .answer_generator import AnswerGenerator
from .context_builder import ContextBuilder
from .numerical_validator import NumericalValidator
from .model_manager import model_manager
from .output_sanitizer import OutputSanitizer
from .trace_collector import TraceCollector
from .reasoning import (
    ReasoningCollector,
    REASONING_STEP_INTENT,
    REASONING_STEP_CONTEXT_REWRITE,
    REASONING_STEP_WEB_SEARCH,
    REASONING_STEP_KB_RETRIEVE,
    REASONING_STEP_TOOL_EXECUTE,
    REASONING_STEP_ANSWER_GENERATE,
    REASONING_STEP_FALLBACK,
)

# 导入 Prometheus 指标模块
try:
    from src.middleware.prometheus import record_vector_search, record_llm_call, record_llm_call_error, record_kb_query
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

from src.utils.async_singleton import AsyncSingleton

# 请求级决策/检索状态。RAGChain 是进程级单例，此前将决策结果挂在
# self 上，并发请求会互相覆盖（串号）。ContextVar 在每个 asyncio Task
# 中独立复制：并发请求互不可见；同一 Task 内 set 后可被后续代码读取。
# 注意：set 与 get 必须发生在同一 Task（不要跨 asyncio.create_task 读取）。
_request_decision: ContextVar[Optional[DecisionResult]] = ContextVar(
    "rag_request_decision", default=None
)
_request_retrieval_score: ContextVar[float] = ContextVar(
    "rag_request_retrieval_score", default=0.0
)


# ----------------------------------------------------------------------
# 提示词模板（流式/非流式共用的单一事实来源，消除两套模板漂移）
# ----------------------------------------------------------------------
KB_ANSWER_TEMPLATE = """
你是一个严谨的智能助手，请基于以下参考信息回答用户问题。

回答要求：
1. 优先使用参考信息中的内容，禁止编造参考信息里不存在的信息。
2. 如果参考信息中没有答案，直接说明"无法找到相关信息"。
3. 关键事实必须标注来源编号，如[1]、[2]，对应参考信息中的来源编号。
4. 如果同时包含知识库和联网搜索结果，优先以知识库内容为准，联网搜索作为补充。
5. 对于价格、日期、数据等时效性信息，优先提取具体数值并突出展示，
   标注数据来源；若多个来源数据不一致，给出范围而非随机选取。
6. **禁止输出任何工具调用 JSON、```json 代码块、<tool_call> 标签或 <RichMediaReference> 思考过程**。
7. **禁止在回答开头使用"根据搜索结果"、"根据参考信息"、"资料显示"、"我认为"等前缀，直接陈述答案**。
8. 当工具结果置信度低于 60% 时，不要以绝对语气给出具体数值，应说明"数据可能存在偏差，建议通过官方渠道核实"。

对话历史:
{history}

参考信息:
{context}

当前问题:
{question}

请结合参考信息给出准确、连贯的回答：
"""

LLM_DIRECT_TEMPLATE = """
你是一个智能助手。本次联网搜索未能返回有效结果。
请基于你已有的知识回答用户问题：
- 非时效性问题：正常回答。
- 涉及时效性信息（如价格、新闻、天气、实时数据等）：基于你的知识作答，
  并在回答末尾标注"以上信息基于训练数据，可能不具备实时性，建议核实最新情况"，
  不要简单拒绝或只说"无法获取实时信息"。

回答要求：
1. **禁止在回答开头使用"根据搜索结果"、"根据参考信息"、"资料显示"、"我认为"等前缀，直接陈述答案**。
2. 保持回答的连贯性和上下文一致性。

对话历史:
{history}

当前问题:
{question}

请结合历史对话进行回答：
"""


@dataclass
class _PipelineState:
    """问答管线各阶段的共享状态（单次请求内有效，不跨请求复用）。

    事件协议（_pipeline 及各阶段 yield 的元组，首元素为事件类型）：
    - ("reasoning", payload_str): reasoning 过程事件（流式对外转发，非流式忽略）
    - ("chunk", text, source_texts, source_metadata, answer_type): 答案片段
    - ("final", state): 终态事件，携带完整状态供 run() 做非流式后处理
    """

    question: str
    kb_ids: Optional[List[str]]
    history: Optional[List[dict]]
    use_web_search: bool
    search_mode: str

    trace: Any = None
    resolved_question: str = ""
    history_context: str = ""

    intent_decision: Any = None
    decision: Optional[DecisionResult] = None
    use_kb: bool = False
    is_agent_mode: bool = False

    docs: List[Any] = field(default_factory=list)
    source_texts: List[str] = field(default_factory=list)
    source_metadata: List[Dict] = field(default_factory=list)
    search_context: str = ""
    cross_source_data: Optional[Dict] = None
    web_sources_for_citation: List[Dict] = field(default_factory=list)
    answer_type: str = "llm_direct"

    # 累积的最终答案（清洗后的全部 chunk 拼接），终态写入 trace
    final_answer: str = ""
    # 阶段置 True 表示流程已产出完整回答（datetime/tool_first/纯 Agent 短路）
    finished: bool = False


class RAGChain(AsyncSingleton["RAGChain"]):
    """RAG 问答链类，结合知识库与 LLM 进行智能问答。"""

    def __init__(self, vector_store=None, strategy_manager=None):
        """初始化 RAG 问答链。

        Args:
            vector_store: VectorStoreManager 实例。
            strategy_manager: 策略管理器实例（可选）。
        """
        self.llm = None
        self.vector_store = vector_store
        self.retriever = None
        self.strategy_manager = strategy_manager
        self.decision_pipeline = None
        self.similarity_threshold = 0.4
        self.sentence_transformer = None
        self.last_reasoning: List[Dict[str, Any]] = []
        self.web_search_service = None
        self.search_toolkit = None
        self.function_calling_handler = None
        self.react_agent = None
        # 阶段二新增：意图路由、工具执行、答案生成
        self.intent_router = None
        self.tool_executor = None
        self.answer_generator = None
        # P0-f：引用补全器与答案校验器（链路整合后处理）
        self.citation_backfiller = None
        self.answer_verifier = None
        # 上下文增强、问题处理、知识库对比等能力委托给独立服务
        self.context_enhancer = None
        self.question_processor = None
        self.kb_comparator = None
        self.document_analyzer = None
        self.kb_recommender = None
        self.knowledge_graph_generator = None

    async def _async_init(self):
        """异步初始化 RAG 问答链。

        依赖的 vector_store 和 strategy_manager 通过 __init__ 传入，
        首次调用时完成一次性初始化。
        """
        if self.vector_store is None:
            self.vector_store = await VectorStoreManager.get_instance()
        self.strategy_manager = self.strategy_manager or await create_default_strategy_manager()
        self.decision_pipeline = DecisionPipeline(self.strategy_manager)

        answer_model = await model_manager.get_model_for_task("answer")
        self.llm = ChatOllama(model=answer_model, streaming=True)

        # 初始化联网搜索服务（注入 LLM 和缓存）
        cache_service = None
        try:
            from src.services.cache_service import CacheService
            cache_service = await CacheService.get_instance()
        except Exception as e:
            logger.warning(f"缓存服务初始化失败，联网搜索将不使用缓存: {e}")
        self.web_search_service = WebSearchService(llm=self.llm, cache_service=cache_service)

        # 初始化 Phase 3 Agent（按需延迟创建 handler，但先创建 toolkit）
        self.search_toolkit = SearchToolkit(self.web_search_service)

        # 初始化阶段二组件
        self.intent_router = IntentRouter()
        self.tool_executor = ToolExecutor(self.search_toolkit.tool_manager)
        self.answer_generator = AnswerGenerator(
            self.llm,
            numerical_validator=NumericalValidator(),
        )
        self.context_builder = ContextBuilder()

        # P0-f：初始化引用补全器与答案校验器（复用 Milvus 服务的 embeddings）
        embeddings = None
        try:
            if self._has_vector_store() and self.vector_store.milvus_service is not None:
                embeddings = self.vector_store.milvus_service.embeddings
        except Exception as e:
            logger.warning(f"获取 embeddings 失败，引用补全将降级为仅校验模式: {e}")
        self.citation_backfiller = None
        self.answer_verifier = None
        try:
            from .citation_backfiller import CitationBackfiller
            from .output_sanitizer import AnswerVerifier
            self.citation_backfiller = CitationBackfiller(embeddings=embeddings)
            self.answer_verifier = AnswerVerifier(embeddings=embeddings, llm=self.llm)
        except Exception as e:
            logger.warning(f"引用补全器/答案校验器初始化失败，将跳过后处理: {e}")

        # 初始化各独立服务（按职责拆分）
        self.context_enhancer = ContextEnhancer(self.llm)
        self.question_processor = QuestionProcessor(self.llm)
        self.kb_comparator = KBComparator(self.llm, self)
        self.document_analyzer = DocumentAnalyzer(self.llm, self)
        self.kb_recommender = KBRecommender(self)
        self.knowledge_graph_generator = KnowledgeGraphGenerator(self.llm, self)

    @classmethod
    async def get_instance(cls, vector_store=None, strategy_manager=None) -> "RAGChain":
        """单例获取入口。

        参数仅在首次创建实例时生效，后续调用会忽略参数并返回已有实例。
        """
        return await super().get_instance(vector_store, strategy_manager)

    def _init_similarity_model(self):
        """初始化语义相似度模型（延迟加载）"""
        if self.sentence_transformer is None:
            try:
                from sentence_transformers import SentenceTransformer, util
                self.sentence_transformer = SentenceTransformer('all-MiniLM-L6-v2')
                self.similarity_util = util
            except ImportError:
                self.sentence_transformer = None

    async def _calculate_relevance(self, question, docs):
        """
        计算检索文档与问题的相关性
        
        Args:
            question: 用户问题
            docs: 检索到的文档列表
            
        Returns:
            tuple: (avg_score, has_relevant)
                - avg_score: 平均相关度分数
                - has_relevant: 是否有相关文档
        """
        if not docs or len(docs) == 0:
            return 0.0, False
        
        self._init_similarity_model()
        
        if self.sentence_transformer is None:
            return 0.6, True
        
        question_embedding = await asyncio.to_thread(self.sentence_transformer.encode, question)
        total_score = 0.0
        relevant_count = 0
        
        for doc in docs:
            doc_content = doc.page_content if hasattr(doc, 'page_content') else str(doc)
            doc_embedding = await asyncio.to_thread(self.sentence_transformer.encode, doc_content)
            score = float(self.similarity_util.cos_sim(question_embedding, doc_embedding))
            total_score += score
            if score >= self.similarity_threshold:
                relevant_count += 1
        
        avg_score = total_score / len(docs)
        has_relevant = relevant_count > 0 or avg_score >= self.similarity_threshold
        
        return avg_score, has_relevant

    def get_last_retrieval_score(self):
        """
        获取上一次检索的相关性分数
        
        Returns:
            float: 相关性分数（请求级隔离，未经过检索时为 0.0）
        """
        return _request_retrieval_score.get()

    def _get_retriever(self):
        """
        获取或创建检索器（延迟初始化）
        
        当前 VectorStoreManager 使用 MilvusService 直接搜索，不再提供 LangChain retriever，
        因此始终返回 None。文档检索由 _retrieve_documents 直接调用 search 完成。
        
        Returns:
            None
        """
        return None

    def _has_vector_store(self):
        """
        检查向量库是否存在
        
        Returns:
            bool: True表示向量库存在并可用
        """
        return self.vector_store is not None and self.vector_store.milvus_service is not None

    async def _should_use_knowledge_base(self, question, history=None, kb_ids=None, use_web_search=False, search_mode="simple"):
        """
        判断是否需要使用知识库（使用决策管道）

        保持向后兼容的方法，使用决策管道进行判断

        Args:
            question: 用户问题
            history: 历史对话列表
            kb_ids: 选中的知识库ID列表（可选）
            use_web_search: 是否使用联网搜索（可选）
            search_mode: 搜索模式（可选）：simple/function_calling/agent

        Returns:
            bool: True表示需要使用知识库，False表示直接回答
        """
        if not self._has_vector_store():
            return False

        return await self._make_decision(question, kb_ids, history, use_web_search=use_web_search, search_mode=search_mode)

    def get_last_strategy_confidences(self):
        """
        获取上一次策略判断的各策略置信度（保持向后兼容）

        Returns:
            Dict[str, float]: 各策略置信度字典（请求级隔离，本请求未决策时为空）
        """
        decision = _request_decision.get()
        if decision:
            return decision.strategy_results
        return {}

    async def _make_decision(self, question, kb_ids=None, history=None, force_mode=None, use_web_search=False, search_mode="simple"):
        """
        使用决策管道判断是否需要使用知识库
        
        Args:
            question: 用户问题
            kb_ids: 选中的知识库ID列表
            history: 历史对话列表
            force_mode: 强制问答模式
            use_web_search: 是否使用联网搜索
            search_mode: 搜索模式（可选）：simple/function_calling/agent
            
        Returns:
            bool: True表示需要使用知识库，False表示直接回答
        """
        decision = await self.decision_pipeline.decide(
            question, kb_ids, history, force_mode, use_web_search, search_mode
        )
        # 写入请求级 ContextVar：并发请求各自可见自己的决策结果
        _request_decision.set(decision)
        return decision.should_use_kb

    def get_last_decision(self):
        """
        获取上一次的完整决策结果（请求级隔离）

        注意：set 与 get 必须在同一 asyncio Task 内；跨 Task 读取将得到 None。

        Returns:
            DecisionResult or None: 决策结果
        """
        return _request_decision.get()

    def get_last_reasoning(self) -> List[Dict[str, Any]]:
        """
        获取上一次问答链路的 reasoning 步骤列表。

        Returns:
            List[Dict[str, Any]]: reasoning 步骤字典列表
        """
        return self.last_reasoning

    def get_strategy_status(self):
        """
        获取策略管理器状态
        
        Returns:
            Dict[str, Dict[str, float]]: 策略状态字典
        """
        return self.strategy_manager.get_strategy_status()

    def set_strategy_weight(self, strategy_name, weight):
        """
        设置策略权重
        
        Args:
            strategy_name: 策略名称
            weight: 权重值（0.0-1.0）
        """
        self.strategy_manager.set_weight(strategy_name, weight)

    def set_decision_threshold(self, threshold):
        """
        设置决策阈值
        
        Args:
            threshold: 阈值（0.0-1.0）
        """
        self.strategy_manager.set_threshold(threshold)

    async def _retrieve_documents(self, question, kb_ids=None, document_ids=None):
        """
        根据问题检索相关文档。

        阶段一升级：优先使用混合检索（dense + BM25 + RRF + rerank），
        未启用或失败时回退到纯 dense 检索。

        Args:
            question: 用户问题
            kb_ids: 指定的知识库ID列表（可选）
            document_ids: 指定的文档ID列表（可选）

        Returns:
            list: 检索到的Document对象列表
        """
        if not self._has_vector_store():
            return []

        # 记录向量检索开始时间
        search_start = time.time()

        try:
            search_kwargs = {"k": settings.processing.TOP_K}
            if kb_ids and len(kb_ids) > 0:
                search_kwargs["kb_ids"] = kb_ids
            elif document_ids and len(document_ids) > 0:
                search_kwargs["document_ids"] = document_ids

            if settings.processing.KB_ENABLE_HYBRID_SEARCH:
                try:
                    docs = await self.vector_store.search_hybrid(question, **search_kwargs)
                except Exception as e:
                    logger.warning(f"混合检索失败，回退到 dense 检索: {e}")
                    docs = await self.vector_store.search_dense(question, **search_kwargs)
            else:
                docs = await self.vector_store.search_dense(question, **search_kwargs)

            return docs[:settings.processing.TOP_K]
        finally:
            # 记录向量检索时间（Prometheus）
            if PROMETHEUS_AVAILABLE:
                record_vector_search(time.time() - search_start)

    async def _update_conversation_summary(self, history: list) -> str:
        """
        更新对话摘要，用于长对话的上下文管理

        委托给 ContextEnhancer 处理。

        Args:
            history: 历史对话列表

        Returns:
            str: 更新后的对话摘要
        """
        return await self.context_enhancer._update_conversation_summary(history)

    async def enhance_context(self, question: str, history: list) -> dict:
        """
        增强上下文处理，包括指代消解和上下文优化

        委托给 ContextEnhancer 处理。

        Args:
            question: 当前问题
            history: 历史对话列表

        Returns:
            dict: 包含增强后的问题和上下文信息
        """
        return await self.context_enhancer.enhance_context(question, history)

    def _extract_source_info(self, docs):
        """
        从检索到的文档中提取来源信息（支持答案溯源）
        
        Args:
            docs: Document对象列表
            
        Returns:
            tuple: (source_texts, source_metadata)
                - source_texts: 来源文档文本列表
                - source_metadata: 来源元信息列表，包含filename、chunk_index、document_id等
        """
        source_texts = []
        source_metadata = []
        
        for doc in docs:
            source_texts.append(doc.page_content)
            
            metadata = doc.metadata if hasattr(doc, 'metadata') else {}
            source_metadata.append({
                'document_id': metadata.get('document_id', ''),
                'filename': metadata.get('filename', 'unknown'),
                'source': metadata.get('source', ''),
                'chunk_index': metadata.get('chunk_index', 0),
                'total_chunks': metadata.get('total_chunks', 1),
                'page_content': doc.page_content[:200] + '...' if len(doc.page_content) > 200 else doc.page_content
            })
        
        return source_texts, source_metadata

    # ------------------------------------------------------------------
    # Phase 3: Agent 搜索辅助方法
    # ------------------------------------------------------------------
    def _ensure_agents(self):
        """按需初始化 Function Calling / ReAct Agent"""
        if self.search_toolkit is None:
            self.search_toolkit = SearchToolkit(self.web_search_service)

        if self.function_calling_handler is None:
            self.function_calling_handler = FunctionCallingHandler(self.llm, self.search_toolkit)

        if self.react_agent is None:
            self.react_agent = ReActAgent(self.llm, self.search_toolkit)

    def _web_sources_to_metadata(self, sources: List[Dict]) -> tuple:
        """将 Agent 返回的 web 来源转换为 RAGChain 统一格式"""
        source_texts = []
        source_metadata = []
        for s in sources:
            page_content = s.get("page_content", "")
            source_texts.append(page_content)
            source_metadata.append({
                "document_id": s.get("document_id", ""),
                "filename": s.get("filename", "web_search"),
                "source": s.get("source", ""),
                "chunk_index": s.get("chunk_index", 0),
                "total_chunks": s.get("total_chunks", 1),
                "page_content": page_content,
                "url": s.get("url", ""),
            })
        return source_texts, source_metadata

    def _build_citation_sources(self, web_sources: List[Dict]) -> List[Dict]:
        """将 web 搜索来源转换为 CitationBackfiller/AnswerVerifier 所需格式。

        CitationBackfiller 期望 sources 含 source_index/title/content 字段；
        build_search_context_enhanced 返回的 sources 已含这些字段，
        此方法用于兼容 build_search_context_with_sources 或 Agent 来源。

        Args:
            web_sources: web 搜索来源列表。

        Returns:
            含 source_index/title/content/url 的来源列表。
        """
        citation_sources = []
        for i, s in enumerate(web_sources or [], 1):
            content = s.get("content") or s.get("page_content", "")
            citation_sources.append({
                "source_index": s.get("source_index", i),
                "title": s.get("title", ""),
                "content": content,
                "url": s.get("url", ""),
            })
        return citation_sources

    async def _verify_answer_suffix(
        self,
        answer: str,
        citation_sources: List[Dict],
        cross_source_data: Optional[Dict] = None,
    ) -> tuple:
        """对答案做事实校验并返回警告后缀（P0-f 链路整合）。

        不修改答案文本本身，后缀由调用方决定追加方式：
        流式在生成结束后以 chunk 追加；非流式可拼接后返回。

        Args:
            answer: 待校验的答案文本。
            citation_sources: 供校验使用的来源列表。
            cross_source_data: 多源交叉验证数据（来自 build_search_context_enhanced）。

        Returns:
            (suffix, verification_result):
                - suffix: 警告后缀（无警告时为 None）。
                - verification_result: 校验结果（校验不可用时为 None）。
        """
        if self.answer_verifier is None:
            return None, None
        try:
            verification_result = await self.answer_verifier.verify(
                answer=answer,
                sources=citation_sources,
                cross_source_data=cross_source_data,
            )
            return self.answer_verifier.format_warning_suffix(verification_result), verification_result
        except Exception as e:
            logger.warning(f"答案校验失败，跳过警告追加: {e}")
            return None, None

    @staticmethod
    def _reasoning_payload(step: str, status: str, title: str, content: str = "", duration_ms: Optional[int] = None, metadata: Optional[Dict] = None) -> str:
        """构造 reasoning 事件负载（供 SSE 前端展示搜索/思考过程）。"""
        from .reasoning import ReasoningStep
        return ReasoningStep(
            step=step,
            status=status,
            title=title,
            content=content,
            duration_ms=duration_ms,
            metadata=metadata or {},
        ).to_sse_payload()

    @staticmethod
    def _search_status_payload(status: str, message: str = "", sources: int = 0) -> str:
        """构造搜索状态事件负载（向后兼容，已映射为 reasoning 事件）。"""
        import json
        return json.dumps({"status": status, "message": message, "sources": sources})

    async def _run_agent(self, question: str, history_context: str, mode: QAMode) -> SearchAgentResult:
        """非流式执行 Agent"""
        self._ensure_agents()
        try:
            if mode == QAMode.FUNCTION_CALLING:
                return await self.function_calling_handler.run(question, history_context)
            if mode == QAMode.AGENT_SEARCH:
                return await self.react_agent.run(question, history_context)
        except Exception as e:
            logger.warning(f"Agent 执行失败 [{mode.value}]: {e}")
        return SearchAgentResult(answer="")

    async def _stream_agent(self, question: str, history_context: str, mode: QAMode):
        """流式执行 Agent，yield (chunk, sources)"""
        self._ensure_agents()
        try:
            if mode == QAMode.FUNCTION_CALLING:
                async for chunk, sources in self.function_calling_handler.arun_stream(question, history_context):
                    yield chunk, sources
                return
            if mode == QAMode.AGENT_SEARCH:
                async for chunk, sources in self.react_agent.arun_stream(question, history_context):
                    yield chunk, sources
                return
        except Exception as e:
            logger.warning(f"Agent 流式执行失败 [{mode.value}]: {e}")
        yield "", []

    # ------------------------------------------------------------------
    # 统一问答管线：流式/非流式共用的单一实现。
    # arun_stream() 转发事件给 SSE 消费方；run() 累积 chunk 返回完整结果。
    # ------------------------------------------------------------------
    async def _pipeline(self, question, kb_ids=None, history=None, use_web_search=False, search_mode="simple"):
        """执行完整问答流程，按事件协议产出 reasoning/chunk/final 事件。"""
        state = _PipelineState(
            question=question,
            kb_ids=kb_ids,
            history=history,
            use_web_search=use_web_search,
            search_mode=search_mode,
        )
        state.trace = TraceCollector()
        state.trace.set_basic(question=question)

        # 阶段 1：上下文增强（指代消解）
        await self._stage_enhance_context(state)

        # 阶段 2：时间/日期类问题快捷返回
        datetime_answer = build_datetime_answer(state.resolved_question)
        if datetime_answer:
            if PROMETHEUS_AVAILABLE:
                record_kb_query("datetime_tool")
            state.final_answer = datetime_answer
            yield ("chunk", datetime_answer, [], [], "datetime_tool")
            async for ev in self._finalize(state):
                yield ev
            return

        # 阶段 3：意图路由（含问题改写）
        async for ev in self._stage_intent(state):
            yield ev

        # 阶段 4：工具优先（天气/计算等 tool_first 类）
        async for ev in self._stage_tool_first(state):
            yield ev
        if state.finished:
            async for ev in self._finalize(state):
                yield ev
            return

        # 阶段 5：知识库使用决策
        await self._stage_decide(state)

        # 阶段 6：Agent 模式（纯模式短路 / 混合收集上下文 / Phase 2 降级）
        async for ev in self._stage_agent(state):
            yield ev
        if state.finished:
            async for ev in self._finalize(state):
                yield ev
            return

        # 阶段 7：常规联网搜索（Phase 2）
        async for ev in self._stage_web_search(state):
            yield ev

        # 阶段 8：知识库检索
        async for ev in self._stage_kb_retrieval(state):
            yield ev

        # 阶段 9：构建最终上下文并生成回答
        async for ev in self._stage_generate(state):
            yield ev

        async for ev in self._finalize(state):
            yield ev

    async def _stage_enhance_context(self, state: _PipelineState):
        """阶段 1：上下文增强（指代消解与上下文压缩）。"""
        enhanced = await self.enhance_context(state.question, state.history or [])
        state.resolved_question = enhanced["resolved_question"]
        state.history_context = enhanced["enhanced_context"]
        state.trace.data["resolved_question"] = state.resolved_question

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
        if intent_decision.context_rewrite:
            state.trace.data["context_rewrite"] = intent_decision.context_rewrite
            if intent_decision.context_rewrite != state.resolved_question:
                state.resolved_question = intent_decision.context_rewrite
                logger.info(f"使用意图路由改写后的问题: {state.resolved_question}")
                yield ("reasoning", self._reasoning_payload(
                    REASONING_STEP_CONTEXT_REWRITE,
                    "done",
                    "问题改写",
                    content=f"改写为：{state.resolved_question}",
                    metadata={"rewritten_question": state.resolved_question},
                ))

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

            async for chunk, _ in self.answer_generator.generate_stream(
                question=state.resolved_question,
                history_context=state.history_context,
                tool_results=successful_results,
                is_realtime=intent_decision.needs_realtime,
            ):
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

    async def _stage_agent(self, state: _PipelineState):
        """阶段 6：Agent 模式（Function Calling / ReAct）。

        纯 Agent 模式：流式输出 Agent 回答，空输出或被工具 JSON 污染时
        降级到 Phase 2 联网搜索；Agent + 知识库混合：先收集 web 上下文，
        再与知识库合并生成。
        """
        if not state.is_agent_mode:
            return

        agent_sources = []

        if not state.use_kb:
            # 纯 Agent 模式：直接流式输出 Agent 生成的回答
            answer_type = (
                "function_calling"
                if state.decision.mode == QAMode.FUNCTION_CALLING
                else "agent_search"
            )
            state.answer_type = answer_type
            async for chunk, agent_src in self._stream_agent(
                state.resolved_question, state.history_context, state.decision.mode
            ):
                if chunk:
                    state.final_answer += chunk
                    yield ("chunk", chunk, state.source_texts, state.source_metadata, answer_type)
                agent_sources = agent_src

            # 方案B：Agent 未调用工具（输出为空且无 sources），或 Agent 输出被工具 JSON 污染
            # （deepseek-r1 容易把工具调用 JSON 直接当回答输出），降级到 Phase 2 联网搜索。
            # 适用于时效性问题 Agent 决策失败的场景（如"吕梁天气"未触发 web_search），
            # 确保实时信息一定被搜索，而非由 LLM 笼统回复"无法获取"或输出工具 JSON。
            from src.services.search_agent import looks_like_tool_call
            answer_polluted = looks_like_tool_call(state.final_answer)
            if (not state.final_answer and not agent_sources or answer_polluted) and settings.search.SEARCH_AGENT_FALLBACK_TO_PHASE2:
                if answer_polluted:
                    logger.warning(f"Agent 回答被工具 JSON 污染，降级到 Phase 2 联网搜索: {state.resolved_question}")
                else:
                    logger.info(f"Agent 输出为空，降级到 Phase 2 联网搜索: {state.resolved_question}")
                async for ev in self._phase2_web_search(state):
                    yield ev
                # 不终止，继续走后续 final_context 构建与回答生成逻辑。
                # 重置 is_agent_mode 让 answer_type 正确显示为 web_search。
                state.is_agent_mode = False
            else:
                state.source_texts, state.source_metadata = self._web_sources_to_metadata(agent_sources)
                # 阶段三：保存 Agent 链路（trace 记录清洗后的完整回答）
                cleaned_answer, polluted = OutputSanitizer.sanitize(state.final_answer)
                state.final_answer = cleaned_answer
                state.trace.set_pollution_detected(polluted)
                state.finished = True
            if state.finished:
                return

        # Agent + 知识库混合：先让 Agent 收集 web 上下文，再与知识库合并生成
        agent_result = await self._run_agent(state.resolved_question, state.history_context, state.decision.mode)
        if agent_result.answer or agent_result.context:
            state.search_context = agent_result.context or agent_result.answer
            agent_sources = agent_result.sources
        elif settings.search.SEARCH_AGENT_FALLBACK_TO_PHASE2:
            # 方案B：Agent 未调用工具时降级到 Phase 2 联网搜索
            logger.info(f"Agent 输出为空，降级到 Phase 2 联网搜索: {state.resolved_question}")
            async for ev in self._phase2_web_search(state):
                yield ev
            state.is_agent_mode = False

        if not state.source_texts and not state.source_metadata and agent_sources:
            state.source_texts, state.source_metadata = self._web_sources_to_metadata(agent_sources)
            state.web_sources_for_citation = agent_sources
        logger.info(
            f"Agent 搜索已触发: mode={state.decision.mode.value}, query={state.resolved_question}, "
            f"context_length={len(state.search_context)}, sources={len(state.source_metadata)}"
        )

    async def _phase2_web_search(self, state: _PipelineState):
        """Agent 降级路径的 Phase 2 联网搜索（含 reasoning 事件）。"""
        web_search_start = time.time()
        yield ("reasoning", self._reasoning_payload(
            REASONING_STEP_WEB_SEARCH,
            "running",
            "联网搜索",
            content="正在联网搜索...",
        ))
        try:
            search_context, web_sources, cross_source_data = await self.web_search_service.build_search_context_enhanced(
                state.resolved_question, conversation_context=state.history or []
            )
            state.search_context = search_context
            state.cross_source_data = cross_source_data
            web_search_duration = int((time.time() - web_search_start) * 1000)
            if web_sources:
                web_texts, web_metadata = self._web_sources_to_metadata(web_sources)
                state.source_texts.extend(web_texts)
                state.source_metadata.extend(web_metadata)
                state.web_sources_for_citation = web_sources
                yield ("reasoning", self._reasoning_payload(
                    REASONING_STEP_WEB_SEARCH,
                    "done",
                    "联网搜索",
                    content=f"联网搜索完成，找到 {len(web_sources)} 个来源",
                    duration_ms=web_search_duration,
                    metadata={"sources_count": len(web_sources)},
                ))
            else:
                yield ("reasoning", self._reasoning_payload(
                    REASONING_STEP_WEB_SEARCH,
                    "failed",
                    "联网搜索",
                    content="未找到相关网络结果",
                    duration_ms=web_search_duration,
                ))
        except Exception as e:
            logger.warning(f"Agent 降级搜索失败: {e}")
            yield ("reasoning", self._reasoning_payload(
                REASONING_STEP_WEB_SEARCH,
                "failed",
                "联网搜索",
                content="联网搜索服务不可用",
            ))

    async def _stage_web_search(self, state: _PipelineState):
        """阶段 7：常规联网搜索模式（Phase 2）。"""
        if not (state.decision and state.decision.mode in (QAMode.WEB_SEARCH, QAMode.HYBRID_SEARCH)):
            return

        web_search_start = time.time()
        yield ("reasoning", self._reasoning_payload(
            REASONING_STEP_WEB_SEARCH,
            "running",
            "联网搜索",
            content="正在联网搜索...",
        ))
        try:
            search_context, web_sources, cross_source_data = await self.web_search_service.build_search_context_enhanced(
                state.resolved_question, conversation_context=state.history or []
            )
            state.search_context = search_context
            state.cross_source_data = cross_source_data
            web_search_duration = int((time.time() - web_search_start) * 1000)
            if web_sources:
                web_texts, web_metadata = self._web_sources_to_metadata(web_sources)
                state.source_texts.extend(web_texts)
                state.source_metadata.extend(web_metadata)
                state.web_sources_for_citation = web_sources
                yield ("reasoning", self._reasoning_payload(
                    REASONING_STEP_WEB_SEARCH,
                    "done",
                    "联网搜索",
                    content=f"联网搜索完成，找到 {len(web_sources)} 个来源",
                    duration_ms=web_search_duration,
                    metadata={"sources_count": len(web_sources)},
                ))
            else:
                yield ("reasoning", self._reasoning_payload(
                    REASONING_STEP_WEB_SEARCH,
                    "failed",
                    "联网搜索",
                    content="未找到相关网络结果",
                    duration_ms=web_search_duration,
                ))
            logger.info(
                f"联网搜索已触发: mode={state.decision.mode.value}, query={state.resolved_question}, "
                f"context_length={len(state.search_context)}, sources={len(web_sources)}"
            )
        except Exception as e:
            logger.warning(f"联网搜索失败: {e}")
            yield ("reasoning", self._reasoning_payload(
                REASONING_STEP_WEB_SEARCH,
                "failed",
                "联网搜索",
                content="联网搜索服务不可用",
            ))

    async def _stage_kb_retrieval(self, state: _PipelineState):
        """阶段 8：知识库检索与相关性评估。"""
        if not (state.use_kb and state.decision and state.decision.mode in (
            QAMode.PURE_KB, QAMode.HYBRID_INTELLIGENT, QAMode.HYBRID_SEARCH,
            QAMode.FUNCTION_CALLING, QAMode.AGENT_SEARCH
        )):
            return

        yield ("reasoning", self._reasoning_payload(
            REASONING_STEP_KB_RETRIEVE,
            "running",
            "知识库检索",
            content="正在检索知识库...",
        ))
        kb_search_start = time.time()
        state.docs = await self._retrieve_documents(state.resolved_question, state.kb_ids)

        kb_search_duration = int((time.time() - kb_search_start) * 1000)
        if state.docs and len(state.docs) > 0:
            _retrieval_score, has_relevant = await self._calculate_relevance(state.resolved_question, state.docs)
            _request_retrieval_score.set(_retrieval_score)

            if has_relevant:
                doc_texts, doc_metadata = self._extract_source_info(state.docs)
                state.source_texts.extend(doc_texts)
                state.source_metadata.extend(doc_metadata)
                yield ("reasoning", self._reasoning_payload(
                    REASONING_STEP_KB_RETRIEVE,
                    "done",
                    "知识库检索",
                    content=f"知识库检索完成，命中 {len(doc_metadata)} 个片段",
                    duration_ms=kb_search_duration,
                    metadata={"sources_count": len(doc_metadata)},
                ))
            else:
                state.docs = []
                yield ("reasoning", self._reasoning_payload(
                    REASONING_STEP_KB_RETRIEVE,
                    "done",
                    "知识库检索",
                    content="知识库检索完成，未找到高度相关内容",
                    duration_ms=kb_search_duration,
                    metadata={"sources_count": 0},
                ))
        else:
            yield ("reasoning", self._reasoning_payload(
                REASONING_STEP_KB_RETRIEVE,
                "done",
                "知识库检索",
                content="知识库检索完成，未找到相关内容",
                duration_ms=kb_search_duration,
                metadata={"sources_count": 0},
            ))

    async def _stage_generate(self, state: _PipelineState):
        """阶段 9：构建最终上下文（ContextBuilder）并流式生成回答。"""
        web_sources_for_context = list(state.web_sources_for_citation) if state.web_sources_for_citation else []
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
            kb_docs=state.docs,
            web_sources=web_sources_for_context,
        )
        # 用 ContextBuilder 返回的统一编号来源替换旧的 source_texts/source_metadata
        if numbered_sources:
            state.source_texts = [s["content"] for s in numbered_sources]
            state.source_metadata = numbered_sources

        if final_context:
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
                context=final_context,
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
                prompt, state.source_texts, state.source_metadata, answer_type
            ):
                cleaned_chunk, polluted = OutputSanitizer.sanitize(chunk)
                if polluted:
                    state.trace.set_pollution_detected(True)
                if cleaned_chunk:
                    state.final_answer += cleaned_chunk
                    yield ("chunk", cleaned_chunk, source_texts, source_metadata, at)
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
                except Exception as e:
                    logger.warning(f"流式答案校验失败，跳过警告追加: {e}")
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
                prompt, [], [], "llm_direct"
            ):
                cleaned_chunk, polluted = OutputSanitizer.sanitize(chunk)
                if polluted:
                    state.trace.set_pollution_detected(True)
                if cleaned_chunk:
                    state.final_answer += cleaned_chunk
                    yield ("chunk", cleaned_chunk, source_texts, source_metadata, at)
            answer_generate_duration = int((time.time() - answer_generate_start) * 1000)
            yield ("reasoning", self._reasoning_payload(
                REASONING_STEP_ANSWER_GENERATE,
                "done",
                "生成回答",
                content="回答生成完成",
                duration_ms=answer_generate_duration,
            ))

    async def _finalize(self, state: _PipelineState):
        """终态：链路追踪落盘 + 对话摘要更新。"""
        state.trace.set_final_answer(state.final_answer)
        state.trace.finish()
        state.trace.save_background()
        await self._update_conversation_summary(state.history or [])
        yield ("final", state)

    async def arun_stream(self, question, kb_ids=None, history=None, force_mode=None, use_web_search=False, search_mode="simple"):
        """
        流式运行RAG问答。

        转发统一管线 _pipeline 的事件并保持 4 元组公开 API 不变：
        - ("reasoning", payload) → (payload, [], [], "reasoning")
        - ("chunk", text, sources, meta, type) → 原样转发
        - ("final", state) → 仅供 run() 消费，不对外产出

        Yields:
            tuple: (chunk_content, source_texts, source_metadata, answer_type)
                - chunk_content: 回答片段或 reasoning JSON 负载
                - source_texts: 来源文档文本列表
                - source_metadata: 来源元信息列表（包含filename、chunk_index等）
                - answer_type: 回答类型（"knowledge_base"、"llm_direct"、"web_search"、"hybrid_search"、"function_calling"、"agent_search"或"reasoning"）
        """
        async for ev in self._pipeline(
            question, kb_ids=kb_ids, history=history,
            use_web_search=use_web_search, search_mode=search_mode,
        ):
            kind = ev[0]
            if kind == "chunk":
                yield ev[1], ev[2], ev[3], ev[4]
            elif kind == "reasoning":
                yield ev[1], [], [], "reasoning"

    async def _stream_with_retry(self, prompt, source_texts, source_metadata, answer_type, max_retries=2):
        """
        带重试机制的续传式流式生成

        中途失败重试时携带已输出内容作为续写上下文，仅产出新增部分，
        避免整体重发 prompt 导致用户看到重复内容。

        Args:
            prompt: 提示词
            source_texts: 来源文本列表
            source_metadata: 来源元信息列表
            answer_type: 回答类型
            max_retries: 最大重试次数

        Yields:
            tuple: (chunk_content, source_texts, source_metadata, answer_type)
        """
        llm_start = time.time()
        yielded_part = ""

        for attempt in range(max_retries):
            current_prompt = prompt
            if yielded_part:
                current_prompt = (
                    f"{prompt}\n\n"
                    "你上一次的回答因异常中断。已输出的部分如下，"
                    "请从中断处直接继续输出剩余内容，禁止重复任何已输出内容：\n"
                    f"{yielded_part}\n\n请继续："
                )
            try:
                async for chunk in self.llm.astream(current_prompt):
                    if chunk.content:
                        yielded_part += chunk.content
                        yield chunk.content, source_texts, source_metadata, answer_type

                # 记录 LLM 调用时间（Prometheus）
                if PROMETHEUS_AVAILABLE:
                    record_llm_call(settings.model.OLLAMA_MODEL_NAME, time.time() - llm_start)

                return
            except Exception as e:
                if PROMETHEUS_AVAILABLE:
                    record_llm_call_error(settings.model.OLLAMA_MODEL_NAME)
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
                    continue
                error_msg = f"模型调用失败: {str(e)}"
                yield error_msg, source_texts, source_metadata, "error"

    async def run(self, question, kb_ids=None, history=None, force_mode=None, use_web_search=False, search_mode="simple"):
        """
        非流式运行RAG问答。

        复用 _pipeline 单一实现：累积全部答案 chunk 返回完整结果，
        reasoning 过程事件在此路径被忽略。流式/非流式因此共享同一套
        阶段逻辑、提示词模板与续传式重试。

        Args:
            question: 用户问题
            kb_ids: 指定的知识库ID列表（可选）
            history: 历史消息列表（可选），每个消息包含role和content
            force_mode: 强制问答模式（可选，保留签名兼容）
            use_web_search: 是否使用联网搜索（可选）
            search_mode: 搜索模式（可选）：simple/function_calling/agent

        Returns:
            tuple: (answer, sources, source_metadata, answer_type)
                - answer: 完整回答文本
                - sources: 来源文档文本列表
                - source_metadata: 来源元信息列表（包含filename、chunk_index等）
                - answer_type: 回答类型（"knowledge_base"、"llm_direct"、"web_search"、"hybrid_search"、"function_calling"或"agent_search"）
        """
        chunks: List[str] = []
        source_texts: List[str] = []
        source_metadata: List[Dict] = []
        answer_type = "llm_direct"
        state: Optional[_PipelineState] = None

        async for ev in self._pipeline(
            question, kb_ids=kb_ids, history=history,
            use_web_search=use_web_search, search_mode=search_mode,
        ):
            kind = ev[0]
            if kind == "chunk":
                text, st, sm, at = ev[1], ev[2], ev[3], ev[4]
                if text:
                    chunks.append(text)
                source_texts, source_metadata, answer_type = st, sm, at
            elif kind == "final":
                state = ev[1]

        answer = "".join(chunks)

        # 非流式增强：引用补全（校验/回填内联 [n]/[?] 标注）。
        # 事实校验警告后缀已由管线在生成结束后统一追加，此处不再重复。
        if state is not None and state.web_sources_for_citation and self.citation_backfiller is not None:
            try:
                citation_sources = self._build_citation_sources(state.web_sources_for_citation)
                answer = await self.citation_backfiller.backfill(answer, citation_sources)
            except Exception as e:
                logger.warning(f"引用补全失败，保留原始答案: {e}")

        return answer, source_texts, source_metadata, answer_type

    async def rewrite_question(self, question: str) -> dict:
        """
        重写问题，使其更加清晰、完整

        委托给 QuestionProcessor 处理。

        Args:
            question: 原始问题

        Returns:
            dict: {
                "rewritten": 重写后的问题,
                "original": 原始问题,
                "changes": 改动说明
            }
        """
        return await self.question_processor.rewrite_question(question)

    async def classify_question(self, question: str) -> dict:
        """
        识别问题类型

        委托给 QuestionProcessor 处理。

        Args:
            question: 用户问题

        Returns:
            dict: {
                "type": 问题类型,
                "subtype": 子类型,
                "confidence": 置信度,
                "description": 类型说明
            }
        """
        return await self.question_processor.classify_question(question)

    async def compare_knowledge_bases(self, question: str, kb_ids: List[str], kb_name_map: dict = None) -> dict:
        """
        对比多个知识库的答案差异

        委托给 KBComparator 处理。

        Args:
            question: 用户问题
            kb_ids: 知识库ID列表
            kb_name_map: 知识库名称映射（可选）

        Returns:
            dict: 各知识库的答案对比，包含差异分析
        """
        return await self.kb_comparator.compare_knowledge_bases(question, kb_ids, kb_name_map)

    async def generate_suggestions(self, question=None, history_context="", kb_ids=None):
        """
        根据上下文生成智能推荐问题

        委托给 QuestionProcessor 处理。

        Args:
            question: 当前问题（可选）
            history_context: 历史对话上下文（可选）
            kb_ids: 指定的知识库ID列表（可选）

        Returns:
            list: 推荐问题列表（字符串数组）
        """
        return await self.question_processor.generate_suggestions(question, history_context, kb_ids)

    async def recommend_knowledge_bases(self, question: str, top_k: int = 3) -> list:
        """
        基于问题自动推荐相关知识库

        委托给 KBRecommender 处理。

        Args:
            question: 用户问题
            top_k: 返回的知识库数量

        Returns:
            list: 推荐的知识库列表，按相关性排序
        """
        return await self.kb_recommender.recommend_knowledge_bases(question, top_k)

    async def detect_duplicates(self, content: str, kb_id: str = None, threshold: float = 0.85) -> list:
        """
        检测重复或相似文档

        委托给 DocumentAnalyzer 处理。

        Args:
            content: 待检测的文档内容
            kb_id: 限定知识库ID（可选）
            threshold: 相似度阈值（0-1）

        Returns:
            list: 相似文档列表
        """
        return await self.document_analyzer.detect_duplicates(content, kb_id, threshold)

    async def evaluate_document_quality(self, content: str) -> dict:
        """
        评估文档质量和完整性

        委托给 DocumentAnalyzer 处理。

        Args:
            content: 文档内容

        Returns:
            dict: 质量评估结果
        """
        return await self.document_analyzer.evaluate_document_quality(content)

    async def classify_document(self, content: str) -> dict:
        """
        自动识别文档类型和主题

        委托给 DocumentAnalyzer 处理。

        Args:
            content: 文档内容

        Returns:
            dict: 分类结果
        """
        return await self.document_analyzer.classify_document(content)

    async def generate_knowledge_graph(self, kb_ids: list = None) -> dict:
        """
        生成知识库关系图谱

        委托给 KnowledgeGraphGenerator 处理。

        Args:
            kb_ids: 知识库ID列表（可选）

        Returns:
            dict: 知识图谱数据
        """
        return await self.knowledge_graph_generator.generate_knowledge_graph(kb_ids)

    async def close(self):
        """
        清理资源
        """
        if hasattr(self.strategy_manager, 'cleanup_all'):
            await self.strategy_manager.cleanup_all()
