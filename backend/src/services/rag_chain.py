"""RAG 问答链 - 检索增强生成核心模块。

负责问题类型判断、知识库检索、上下文构建与 LLM 流式生成回答。
将上下文增强、问题处理、知识库对比等职责委托给独立服务模块。
"""

import time
import asyncio
import logging
from typing import List, Dict
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

# 导入 Prometheus 指标模块
try:
    from src.middleware.prometheus import record_vector_search, record_llm_call, record_kb_query
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

class RAGChain:
    """RAG 问答链类，结合知识库与 LLM 进行智能问答。"""

    _instance = None
    _lock = asyncio.Lock()
    _initialized = False

    def __init__(self, vector_store, strategy_manager=None):
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
        self.last_decision = None
        self.last_strategy_confidences = {}
        self.similarity_threshold = 0.4
        self.sentence_transformer = None
        self.last_retrieval_score = 0.0
        self.web_search_service = None
        self.search_toolkit = None
        self.function_calling_handler = None
        self.react_agent = None
        # 上下文增强、问题处理、知识库对比等能力委托给独立服务
        self.context_enhancer = None
        self.question_processor = None
        self.kb_comparator = None
        self.document_analyzer = None
        self.kb_recommender = None
        self.knowledge_graph_generator = None

    async def _async_init(self):
        async with RAGChain._lock:
            if RAGChain._initialized:
                return
            self.llm = ChatOllama(model=settings.model.OLLAMA_MODEL_NAME, streaming=True)
            self.strategy_manager = self.strategy_manager or create_default_strategy_manager()
            self.decision_pipeline = DecisionPipeline(self.strategy_manager)

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

            # 初始化各独立服务（按职责拆分）
            self.context_enhancer = ContextEnhancer(self.llm)
            self.question_processor = QuestionProcessor(self.llm)
            self.kb_comparator = KBComparator(self.llm, self)
            self.document_analyzer = DocumentAnalyzer(self.llm, self)
            self.kb_recommender = KBRecommender(self)
            self.knowledge_graph_generator = KnowledgeGraphGenerator(self.llm, self)

            RAGChain._initialized = True

    @classmethod
    async def get_instance(cls, vector_store, strategy_manager=None) -> "RAGChain":
        """单例获取入口，含加锁与初始化协调。"""
        if cls._instance is None:
            cls._instance = cls(vector_store, strategy_manager)
        await cls._instance._async_init()
        return cls._instance

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
            float: 相关性分数
        """
        return self.last_retrieval_score

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

    def _should_use_knowledge_base(self, question, history=None, kb_ids=None, use_web_search=False, search_mode="simple"):
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

        return self._make_decision(question, kb_ids, history, use_web_search=use_web_search, search_mode=search_mode)

    def get_last_strategy_confidences(self):
        """
        获取上一次策略判断的各策略置信度（保持向后兼容）
        
        Returns:
            Dict[str, float]: 各策略置信度字典
        """
        if self.last_decision:
            return self.last_decision.strategy_results
        return self.last_strategy_confidences

    def _make_decision(self, question, kb_ids=None, history=None, force_mode=None, use_web_search=False, search_mode="simple"):
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
        self.last_decision = self.decision_pipeline.decide(
            question, kb_ids, history, force_mode, use_web_search, search_mode
        )
        # 保持向后兼容，更新 last_strategy_confidences
        self.last_strategy_confidences = self.last_decision.strategy_results
        return self.last_decision.should_use_kb

    def get_last_decision(self):
        """
        获取上一次的完整决策结果
        
        Returns:
            DecisionResult or None: 决策结果
        """
        return self.last_decision

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
        根据问题检索相关文档
        
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
            if kb_ids and len(kb_ids) > 0:
                docs = await self.vector_store.search(question, k=settings.processing.TOP_K, kb_ids=kb_ids)
                return docs[:settings.processing.TOP_K]
            elif document_ids and len(document_ids) > 0:
                docs = await self.vector_store.search(question, k=settings.processing.TOP_K, document_ids=document_ids)
                return docs[:settings.processing.TOP_K]
            else:
                docs = await self.vector_store.search(question, k=settings.processing.TOP_K)
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

    async def arun_stream(self, question, kb_ids=None, history=None, force_mode=None, use_web_search=False, search_mode="simple"):
        """
        流式运行RAG问答（支持多轮对话优化）
        
        生成器函数，逐块返回回答内容
        支持检索结果质量评估和自动降级机制
        支持联网搜索功能
        支持指代消解和上下文压缩
        
        Args:
            question: 用户问题
            kb_ids: 指定的知识库ID列表（可选）
            history: 历史消息列表（可选），每个消息包含role和content
            force_mode: 强制问答模式（可选）
            use_web_search: 是否使用联网搜索（可选）
            
        Yields:
            tuple: (chunk_content, source_texts, source_metadata, answer_type)
                - chunk_content: 回答片段
                - source_texts: 来源文档文本列表
                - source_metadata: 来源元信息列表（包含filename、chunk_index等）
                - answer_type: 回答类型（"knowledge_base"、"llm_direct"、"web_search"、"hybrid_search"、"function_calling"或"agent_search"）
        """
        enhanced = await self.enhance_context(question, history or [])
        resolved_question = enhanced["resolved_question"]
        history_context = enhanced["enhanced_context"]

        strategy_decision = self._should_use_knowledge_base(
            resolved_question, history, kb_ids, use_web_search=use_web_search, search_mode=search_mode
        )

        decision = self.get_last_decision()
        use_kb = strategy_decision

        docs = []
        source_texts = []
        source_metadata = []
        search_context = ""
        is_agent_mode = decision and decision.mode in (QAMode.FUNCTION_CALLING, QAMode.AGENT_SEARCH)

        # ------------------------------------------------------------------
        # Phase 3 Agent 模式
        # ------------------------------------------------------------------
        if is_agent_mode:
            agent_sources = []
            agent_answer = ""

            if not use_kb:
                # 纯 Agent 模式：直接流式输出 Agent 生成的回答
                answer_type = "function_calling" if decision.mode == QAMode.FUNCTION_CALLING else "agent_search"
                async for chunk, agent_src in self._stream_agent(resolved_question, history_context, decision.mode):
                    if chunk:
                        agent_answer += chunk
                        yield chunk, source_texts, source_metadata, answer_type
                    agent_sources = agent_src

                source_texts, source_metadata = self._web_sources_to_metadata(agent_sources)
                await self._update_conversation_summary(history or [])
                return

            # Agent + 知识库混合：先让 Agent 收集 web 上下文，再与知识库合并生成
            agent_result = await self._run_agent(resolved_question, history_context, decision.mode)
            if agent_result.answer or agent_result.context:
                search_context = agent_result.context or agent_result.answer
                agent_sources = agent_result.sources
            elif settings.search.SEARCH_AGENT_FALLBACK_TO_PHASE2:
                search_context, web_sources = await self.web_search_service.build_search_context_with_sources(resolved_question)
                if web_sources:
                    source_texts, source_metadata = self._web_sources_to_metadata(web_sources)

            if not source_texts and not source_metadata and agent_sources:
                source_texts, source_metadata = self._web_sources_to_metadata(agent_sources)
            logger.info(
                f"Agent 搜索已触发: mode={decision.mode.value}, query={resolved_question}, "
                f"context_length={len(search_context)}, sources={len(source_metadata)}"
            )

        # ------------------------------------------------------------------
        # 常规联网搜索模式（Phase 2）
        # ------------------------------------------------------------------
        elif decision and decision.mode in (QAMode.WEB_SEARCH, QAMode.HYBRID_SEARCH):
            search_context, web_sources = await self.web_search_service.build_search_context_with_sources(resolved_question)
            if web_sources:
                web_texts, web_metadata = self._web_sources_to_metadata(web_sources)
                source_texts.extend(web_texts)
                source_metadata.extend(web_metadata)
            logger.info(f"联网搜索已触发: mode={decision.mode.value}, query={resolved_question}, context_length={len(search_context)}, sources={len(web_sources)}")

        # ------------------------------------------------------------------
        # 知识库检索
        # ------------------------------------------------------------------
        if use_kb and decision and decision.mode in (
            QAMode.PURE_KB, QAMode.HYBRID_INTELLIGENT, QAMode.HYBRID_SEARCH,
            QAMode.FUNCTION_CALLING, QAMode.AGENT_SEARCH
        ):
            docs = await self._retrieve_documents(resolved_question, kb_ids)

            if docs and len(docs) > 0:
                self.last_retrieval_score, has_relevant = await self._calculate_relevance(resolved_question, docs)

                if has_relevant:
                    doc_texts, doc_metadata = self._extract_source_info(docs)
                    source_texts.extend(doc_texts)
                    source_metadata.extend(doc_metadata)
                else:
                    docs = []

        # ------------------------------------------------------------------
        # 构建最终上下文并生成回答
        # ------------------------------------------------------------------
        all_context_parts = []
        if search_context:
            all_context_parts.append(f"联网搜索结果:\n{search_context}")
        if docs:
            formatted_docs = "\n\n".join(doc.page_content for doc in docs)
            all_context_parts.append(f"知识库文档:\n{formatted_docs}")

        final_context = "\n\n".join(all_context_parts)

        if final_context:
            if PROMETHEUS_AVAILABLE:
                if search_context and docs:
                    record_kb_query("hybrid_search")
                elif search_context:
                    record_kb_query("web_search")
                else:
                    record_kb_query("knowledge_base")

            answer_type = "web_search" if search_context else "knowledge_base"
            if is_agent_mode:
                answer_type = "function_calling" if decision.mode == QAMode.FUNCTION_CALLING else "agent_search"
            elif search_context and docs:
                answer_type = "hybrid_search"

            template = """
            你是一个严谨的智能助手，请严格基于以下参考信息回答用户问题。

            回答要求：
            1. 只使用参考信息中的内容，禁止编造参考信息里不存在的信息。
            2. 如果参考信息中没有答案，直接说明"无法找到相关信息"。
            3. 关键事实必须标注来源编号，如[1]、[2]，对应参考信息中的来源编号。
            4. 如果同时包含知识库和联网搜索结果，优先以知识库内容为准，联网搜索作为补充。

            对话历史:
            {history}

            参考信息:
            {context}

            当前问题:
            {question}

            请结合参考信息给出准确、连贯的回答：
            """
            prompt = template.format(history=history_context, context=final_context, question=resolved_question)
            async for chunk_tuple in self._stream_with_retry(prompt, source_texts, source_metadata, answer_type):
                yield chunk_tuple
        else:
            if PROMETHEUS_AVAILABLE:
                record_kb_query("llm_direct")

            template = """
            你是一个智能助手，请回答用户的问题。

            对话历史:
            {history}

            当前问题:
            {question}

            请结合历史对话进行回答，保持回答的连贯性和上下文一致性。
            """
            prompt = template.format(history=history_context, question=resolved_question)
            async for chunk_tuple in self._stream_with_retry(prompt, [], [], "llm_direct"):
                yield chunk_tuple

        await self._update_conversation_summary(history or [])

    async def _stream_with_retry(self, prompt, source_texts, source_metadata, answer_type, max_retries=2):
        """
        带重试机制的流式生成
        
        Args:
            prompt: 提示词
            source_texts: 来源文本列表
            source_metadata: 来源元信息列表
            answer_type: 回答类型
            max_retries: 最大重试次数
            
        Yields:
            tuple: (chunk_content, source_texts, source_metadata, answer_type)
        """
        # 记录 LLM 调用开始时间
        llm_start = time.time()
        
        for attempt in range(max_retries):
            try:
                async for chunk in self.llm.astream(prompt):
                    yield chunk.content, source_texts, source_metadata, answer_type
                
                # 记录 LLM 调用时间（Prometheus）
                if PROMETHEUS_AVAILABLE:
                    record_llm_call(settings.model.OLLAMA_MODEL_NAME, time.time() - llm_start)
                
                return
            except Exception as e:
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
                    continue
                else:
                    error_msg = f"模型调用失败: {str(e)}"
                    yield error_msg, source_texts, source_metadata, "error"

    async def run(self, question, kb_ids=None, history=None, force_mode=None, use_web_search=False, search_mode="simple"):
        """
        非流式运行RAG问答（支持多轮对话优化）

        支持检索结果质量评估和自动降级机制
        支持联网搜索功能
        支持指代消解和上下文压缩
        支持 Phase 3 Function Calling / ReAct Agent 模式

        Args:
            question: 用户问题
            kb_ids: 指定的知识库ID列表（可选）
            history: 历史消息列表（可选），每个消息包含role和content
            force_mode: 强制问答模式（可选）
            use_web_search: 是否使用联网搜索（可选）
            search_mode: 搜索模式（可选）：simple/function_calling/agent

        Returns:
            tuple: (answer, sources, source_metadata, answer_type)
                - answer: 完整回答文本
                - sources: 来源文档文本列表
                - source_metadata: 来源元信息列表（包含filename、chunk_index等）
                - answer_type: 回答类型（"knowledge_base"、"llm_direct"、"web_search"、"hybrid_search"、"function_calling"或"agent_search"）
        """
        enhanced = await self.enhance_context(question, history or [])
        resolved_question = enhanced["resolved_question"]
        history_context = enhanced["enhanced_context"]

        strategy_decision = self._should_use_knowledge_base(
            resolved_question, history, kb_ids, use_web_search=use_web_search, search_mode=search_mode
        )

        decision = self.get_last_decision()
        use_kb = strategy_decision

        docs = []
        source_texts = []
        source_metadata = []
        search_context = ""
        is_agent_mode = decision and decision.mode in (QAMode.FUNCTION_CALLING, QAMode.AGENT_SEARCH)

        # ------------------------------------------------------------------
        # Phase 3 Agent 模式
        # ------------------------------------------------------------------
        if is_agent_mode:
            agent_sources = []
            agent_result = await self._run_agent(resolved_question, history_context, decision.mode)

            if agent_result.answer and not use_kb:
                # 纯 Agent 模式：直接返回 Agent 生成的答案
                source_texts, source_metadata = self._web_sources_to_metadata(agent_result.sources)
                await self._update_conversation_summary(history or [])
                answer_type = "function_calling" if decision.mode == QAMode.FUNCTION_CALLING else "agent_search"
                return agent_result.answer, source_texts, source_metadata, answer_type

            if agent_result.answer or agent_result.context:
                search_context = agent_result.context or agent_result.answer
                agent_sources = agent_result.sources
            elif settings.search.SEARCH_AGENT_FALLBACK_TO_PHASE2:
                search_context, web_sources = await self.web_search_service.build_search_context_with_sources(resolved_question)
                if web_sources:
                    source_texts, source_metadata = self._web_sources_to_metadata(web_sources)

            if not source_texts and not source_metadata and agent_sources:
                source_texts, source_metadata = self._web_sources_to_metadata(agent_sources)
            logger.info(
                f"Agent 搜索已触发: mode={decision.mode.value}, query={resolved_question}, "
                f"context_length={len(search_context)}, sources={len(source_metadata)}"
            )

        # ------------------------------------------------------------------
        # 常规联网搜索模式（Phase 2）
        # ------------------------------------------------------------------
        elif decision and decision.mode in (QAMode.WEB_SEARCH, QAMode.HYBRID_SEARCH):
            search_context, web_sources = await self.web_search_service.build_search_context_with_sources(resolved_question)
            if web_sources:
                web_texts, web_metadata = self._web_sources_to_metadata(web_sources)
                source_texts.extend(web_texts)
                source_metadata.extend(web_metadata)
            logger.info(f"联网搜索已触发: mode={decision.mode.value}, query={resolved_question}, context_length={len(search_context)}, sources={len(web_sources)}")

        # ------------------------------------------------------------------
        # 知识库检索
        # ------------------------------------------------------------------
        if use_kb and decision and decision.mode in (
            QAMode.PURE_KB, QAMode.HYBRID_INTELLIGENT, QAMode.HYBRID_SEARCH,
            QAMode.FUNCTION_CALLING, QAMode.AGENT_SEARCH
        ):
            docs = await self._retrieve_documents(resolved_question, kb_ids)

            if docs and len(docs) > 0:
                self.last_retrieval_score, has_relevant = await self._calculate_relevance(resolved_question, docs)

                if has_relevant:
                    doc_texts, doc_metadata = self._extract_source_info(docs)
                    source_texts.extend(doc_texts)
                    source_metadata.extend(doc_metadata)
                else:
                    docs = []

        # ------------------------------------------------------------------
        # 构建最终上下文并生成回答
        # ------------------------------------------------------------------
        all_context_parts = []
        if search_context:
            all_context_parts.append(f"联网搜索结果:\n{search_context}")
        if docs:
            formatted_docs = "\n\n".join(doc.page_content for doc in docs)
            all_context_parts.append(f"知识库文档:\n{formatted_docs}")

        final_context = "\n\n".join(all_context_parts)

        if final_context:
            answer_type = "web_search" if search_context else "knowledge_base"
            if is_agent_mode:
                answer_type = "function_calling" if decision.mode == QAMode.FUNCTION_CALLING else "agent_search"
            elif search_context and docs:
                answer_type = "hybrid_search"

            template = """
            你是一个严谨的智能助手，请严格基于以下参考信息回答用户问题。

            回答要求：
            1. 只使用参考信息中的内容，禁止编造参考信息里不存在的信息。
            2. 如果参考信息中没有答案，直接说明"无法找到相关信息"。
            3. 关键事实必须标注来源编号，如[1]、[2]，对应参考信息中的来源编号。
            4. 如果同时包含知识库和联网搜索结果，优先以知识库内容为准，联网搜索作为补充。

            对话历史:
            {history}

            参考信息:
            {context}

            当前问题:
            {question}

            请结合参考信息给出准确、连贯的回答：
            """
            prompt = template.format(history=history_context, context=final_context, question=resolved_question)
            answer = await self.llm.ainvoke(prompt)

            await self._update_conversation_summary(history or [])
            return answer.content, source_texts, source_metadata, answer_type
        else:
            template = """
            你是一个智能助手，请回答用户的问题。

            对话历史:
            {history}

            当前问题:
            {question}

            请结合历史对话进行回答，保持回答的连贯性和上下文一致性。
            """
            prompt = template.format(history=history_context, question=resolved_question)
            answer = await self.llm.ainvoke(prompt)

            await self._update_conversation_summary(history or [])
            return answer.content, [], [], "llm_direct"

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

    def close(self):
        """
        清理资源
        """
        if hasattr(self.strategy_manager, 'cleanup_all'):
            self.strategy_manager.cleanup_all()
