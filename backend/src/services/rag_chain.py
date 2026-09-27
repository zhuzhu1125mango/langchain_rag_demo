"""RAG 问答链 - 检索增强生成核心模块（编排门面）。

本模块自 2026-09-23 架构还债起按职责拆分到 `src/services/pipeline/` 子包：
- pipeline/state.py      共享状态、提示词模板、请求级 ContextVar
- pipeline/retrieval.py  检索 / 决策 / 上下文增强（RetrievalMixin）
- pipeline/agent.py      Agent 与联网搜索（AgentMixin）
- pipeline/generation.py 流式续传 / 答案校验（GenerationMixin）
- pipeline/stages.py     管线阶段 1-9 与终态清理（PipelineStagesMixin）
- pipeline/insights.py   知识服务薄委托（InsightsMixin）

本文件保留：类构造与依赖装配（__init__/_async_init）、生命周期
（get_instance/close）与公开 API（run/arun_stream），并再导出历史
模块级符号保持既有导入路径兼容。
"""

import logging
from typing import Any, Dict, List, Optional

from langchain_ollama import ChatOllama

from src.config import settings
from src.utils.async_singleton import AsyncSingleton
from .agent_orchestrator import AgentOrchestrator
from .answer_generator import AnswerGenerator
from .context_builder import ContextBuilder
from .context_enhancer import ContextEnhancer
from .decision_pipeline import DecisionPipeline
from .document_analyzer import DocumentAnalyzer
from .intent_router import IntentRouter
from .kb_comparator import KBComparator
from .kb_recommender import KBRecommender
from .kb_retrieval_service import KBRetrievalService
from .knowledge_graph_generator import KnowledgeGraphGenerator
from .model_manager import model_manager
from .numerical_validator import NumericalValidator
from .pipeline.agent import AgentMixin
from .pipeline.generation import GenerationMixin
from .pipeline.insights import InsightsMixin
from .pipeline.retrieval import RetrievalMixin
from .pipeline.stages import PipelineStagesMixin
from .pipeline.state import (  # 再导出：保持历史导入路径兼容
    KB_ANSWER_TEMPLATE,
    LLM_DIRECT_TEMPLATE,
    _PipelineState,
    _StageTimer,
    _background_store_tasks,
    _memory_doc_id,
    _request_decision,
    _request_retrieval_score,
    should_think,
)
from .question_processor import QuestionProcessor
from .query_rewriter import QueryRewriter
from .search_agent import SearchToolkit
from .strategy_manager import create_default_strategy_manager
from .tool_executor import ToolExecutor
from .vector_store import VectorStoreManager
from .web_search_service import WebSearchService

logger = logging.getLogger("rag_system")

__all__ = [
    "RAGChain",
    "should_think",
    "_PipelineState",
    "_StageTimer",
    "_memory_doc_id",
    "_request_decision",
    "_request_retrieval_score",
    "KB_ANSWER_TEMPLATE",
    "LLM_DIRECT_TEMPLATE",
]


class RAGChain(
    AsyncSingleton["RAGChain"],
    RetrievalMixin,
    AgentMixin,
    GenerationMixin,
    PipelineStagesMixin,
    InsightsMixin,
):
    """RAG 问答链类，结合知识库与 LLM 进行智能问答。"""

    def __init__(self, vector_store=None, strategy_manager=None):
        """初始化 RAG 问答链。

        Args:
            vector_store: VectorStoreManager 实例。
            strategy_manager: 策略管理器实例（可选）。
        """
        self.llm = None
        # 深度思考关闭时的非思考专用模型（_async_init 中按配置创建）
        self.llm_direct = None
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
        # P2：有界 Agent 循环编排器（_async_init 中创建）
        self.agent_orchestrator = None
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
        # 统一知识库检索服务（管线与 Agent 工具共用同一检索口径，P1-1）
        self.kb_retrieval_service = None
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
        # 统一检索服务（注入同一 vector_store 实例，保证口径一致）
        self.kb_retrieval_service = KBRetrievalService(self.vector_store)

        answer_model = await model_manager.get_model_for_task("answer")
        self.llm = ChatOllama(model=answer_model, streaming=True, num_ctx=settings.model.OLLAMA_NUM_CTX)
        # 深度思考关闭时的非思考专用模型（混合思考模型无法通过提示词真正跳过思考）；
        # 未配置 OLLAMA_DIRECT_MODEL_NAME 时为 None，调用方回退主模型并绑定 reasoning=False
        self.llm_direct = None
        if settings.model.OLLAMA_DIRECT_MODEL_NAME:
            self.llm_direct = ChatOllama(
                model=settings.model.OLLAMA_DIRECT_MODEL_NAME, streaming=True, num_ctx=settings.model.OLLAMA_NUM_CTX
            )

        # 辅助任务统一走 fast 模型并关闭思考（B3/C2），避免占用主模型与空烧思考链
        fast_llm = await model_manager.get_chat_llm("fast", think=False)

        # 初始化联网搜索服务（注入 LLM 和缓存）
        cache_service = None
        try:
            from src.services.cache_service import CacheService
            cache_service = await CacheService.get_instance()
        except Exception as e:
            logger.warning(f"缓存服务初始化失败，联网搜索将不使用缓存: {e}")
        self.web_search_service = WebSearchService(llm=fast_llm, cache_service=cache_service)

        # 初始化 Phase 3 Agent（按需延迟创建 handler，但先创建 toolkit）
        self.search_toolkit = SearchToolkit(self.web_search_service)

        # 初始化阶段二组件
        self.intent_router = IntentRouter()
        # 统一注册表：tool_first 路径可执行全部插件（此前 toolkit 局部注册表
        # 缺天气/金价/汇率等工具，意图路由建议后会静默"未注册"失败）
        from .tools.tool_manager import get_tool_manager
        self.tool_executor = ToolExecutor(get_tool_manager())
        self.answer_generator = AnswerGenerator(
            self.llm,
            numerical_validator=NumericalValidator(),
            llm_direct=self.llm_direct,
        )
        self.context_builder = ContextBuilder()

        # P2：有界 Agent 循环编排器（AGENT_ORCHESTRATOR_ENABLED 灰度开关控制生效）
        try:
            decision_llm = await model_manager.get_chat_llm("fast", think=False)
            from .tools.tool_manager import get_tool_manager
            self.agent_orchestrator = AgentOrchestrator(
                llm=decision_llm,
                tool_manager=get_tool_manager(),
                answer_generator=self.answer_generator,
                metadata_converter=self._web_sources_to_metadata,
            )
        except Exception as e:
            logger.warning(f"AgentOrchestrator 初始化失败，回退旧 Agent 路径: {e}")
            self.agent_orchestrator = None

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
        # P2-2：KB 多查询改写器（LLM 懒加载，规则路径零模型成本）
        self.query_rewriter = QueryRewriter()
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

    async def arun_stream(self, question, kb_ids=None, history=None, force_mode=None, use_web_search=False, search_mode="simple", user_id=None, session_id=None, deep_thinking="off"):
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
                - answer_type: 回答类型（"knowledge_base"、"llm_direct"、"web_search"、"hybrid_search"、"function_calling"、"agent_search"、"reasoning"或"thinking"）
        """
        try:
            async for ev in self._pipeline(
                question, kb_ids=kb_ids, history=history,
                use_web_search=use_web_search, search_mode=search_mode,
                user_id=user_id, session_id=session_id,
                deep_thinking=deep_thinking,
            ):
                kind = ev[0]
                if kind == "chunk":
                    yield ev[1], ev[2], ev[3], ev[4]
                elif kind == "reasoning":
                    yield ev[1], [], [], "reasoning"
                elif kind == "thinking":
                    yield ev[1], [], [], "thinking"
        except GeneratorExit:
            # P2-5：客户端中途断连（异步生成器被 aclose 关闭）触发，正常完成不触发；
            # 计次供 "SSE 流式中断率" 告警，需先让管线 finally 完成 _finalize_side_effects
            from src.middleware.prometheus import record_sse_interrupted
            try:
                record_sse_interrupted()
            except Exception:
                pass
            raise

    async def run(self, question, kb_ids=None, history=None, force_mode=None, use_web_search=False, search_mode="simple", user_id=None, session_id=None):
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
            user_id: 当前用户ID（可选），写入 Trace 归属
            session_id: 会话ID（可选），写入 Trace 归属
            deep_thinking: 深度思考开关（on/off，可选，默认 off）

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
            user_id=user_id, session_id=session_id,
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

    async def close(self):
        """
        清理资源
        """
        if hasattr(self.strategy_manager, 'cleanup_all'):
            await self.strategy_manager.cleanup_all()
