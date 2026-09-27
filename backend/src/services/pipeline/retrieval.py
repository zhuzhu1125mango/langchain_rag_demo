"""检索、决策与上下文相关方法（自 rag_chain.py 拆出的 Mixin）。

包含：语义相关性评估、统一检索入口、知识库使用决策、策略状态读写、
上下文增强委托与来源信息提取。
"""

import asyncio
import logging
from typing import Any, Dict, List

from src.services.kb_retrieval_service import KBRetrievalService
from src.services.pipeline.state import _request_decision, _request_retrieval_score

logger = logging.getLogger("rag_system")


class RetrievalMixin:
    """检索 / 决策 / 上下文增强方法集（由 RAGChain 组装）。"""

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

    async def _retrieve_documents(self, question, kb_ids=None, document_ids=None, query_embedding=None, queries=None):
        """
        根据问题检索相关文档（委托给统一检索服务 KBRetrievalService）。

        阶段一升级：优先使用混合检索（dense + BM25 + RRF + rerank），
        未启用或失败时回退到纯 dense 检索。
        P2-2：queries 提供多个改写查询且混合检索开启时，走多查询并行检索
        （跨查询 RRF 融合，rerank 仍用原始问题）。

        Args:
            question: 用户问题
            kb_ids: 指定的知识库ID列表（可选）
            document_ids: 指定的文档ID列表（可选）
            query_embedding: 预计算的 query 向量（来自语义缓存查找），传入时跳过重复计算
            queries: 改写后的多查询列表（可选，首元素必须为 question）

        Returns:
            list: 检索到的Document对象列表
        """
        if self.kb_retrieval_service is None:
            self.kb_retrieval_service = KBRetrievalService(self.vector_store)
        # 保留原 _has_vector_store 防护：空壳实例（Milvus 未初始化）直接返回空
        if not self._has_vector_store():
            return []
        return await self.kb_retrieval_service.retrieve(
            question,
            kb_ids=kb_ids,
            document_ids=document_ids,
            query_embedding=query_embedding,
            queries=queries,
        )

    async def _update_conversation_summary(self, history: list, session_id: str = "") -> str:
        """
        更新对话摘要，用于长对话的上下文管理

        委托给 ContextEnhancer 处理。

        Args:
            history: 历史对话列表
            session_id: 会话ID（摘要按会话隔离）

        Returns:
            str: 更新后的对话摘要
        """
        return await self.context_enhancer._update_conversation_summary(history, session_id)

    async def enhance_context(self, question: str, history: list, session_id: str = "") -> dict:
        """
        增强上下文处理，包括指代消解和上下文优化

        委托给 ContextEnhancer 处理。

        Args:
            question: 当前问题
            history: 历史对话列表
            session_id: 会话ID（摘要按会话隔离）

        Returns:
            dict: 包含增强后的问题和上下文信息
        """
        return await self.context_enhancer.enhance_context(question, history, session_id)

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
                'source_kind': metadata.get('source_kind', 'raw'),
                'page_content': doc.page_content[:200] + '...' if len(doc.page_content) > 200 else doc.page_content
            })

        return source_texts, source_metadata
