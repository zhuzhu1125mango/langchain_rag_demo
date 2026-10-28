"""知识库检索阶段：混合检索、rerank 与来源归并（CODE-1 拆分自 stages）。"""

import logging
import time

from src.config import settings
from src.services.decision_pipeline import QAMode
from src.services.pipeline.state import (
    _PipelineState,
    _request_retrieval_score,
)
from src.services.reasoning import (
    REASONING_STEP_KB_RETRIEVE,
)
from src.utils.log_context import log_stage

logger = logging.getLogger("rag_system")

from src.utils.error_handling import log_exception

class KbStageMixin:
    """阶段 8 知识库检索，由 PipelineStagesMixin 组合。"""

    @log_stage("kb_retrieval")
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
        # P2-2：多查询改写（开关开启时），改写失败自动回退单查询
        queries = None
        if settings.processing.KB_MULTI_QUERY_ENABLED:
            try:
                rewritten = await self.query_rewriter.rewrite(state.resolved_question)
                if rewritten and len(rewritten) > 1:
                    queries = rewritten
            except Exception as exc:
                # 改写失败即回退单查询；瞬时故障只记 warning，编程错误会升级为 error
                log_exception(
                    logger,
                    exc,
                    action="pipeline.kb.multi_query",
                    message="KB 多查询改写失败，回退单查询",
                )
        state.docs = await self._retrieve_documents(
            state.resolved_question, state.kb_ids,
            query_embedding=state.semantic_cache_embedding,
            queries=queries,
        )

        kb_search_duration = int((time.time() - kb_search_start) * 1000)
        if state.docs and len(state.docs) > 0:
            _retrieval_score, has_relevant = await self._calculate_relevance(state.resolved_question, state.docs)
            _request_retrieval_score.set(_retrieval_score)

            if has_relevant:
                # P3 交叉链接检索扩展（rerank 后、context 组装前追加，开关控制）
                if settings.wiki_compile.WIKI_LINK_EXPANSION:
                    from src.services.wiki_link_expansion import expand_wiki_links

                    state.docs = await expand_wiki_links(state.docs)
                doc_texts, doc_metadata = self._extract_source_info(state.docs)
                state.source_texts.extend(doc_texts)
                state.source_metadata.extend(doc_metadata)
                retrieve_metadata = {"sources_count": len(doc_metadata)}
                if queries:
                    retrieve_metadata["multi_query_count"] = len(queries)
                yield ("reasoning", self._reasoning_payload(
                    REASONING_STEP_KB_RETRIEVE,
                    "done",
                    "知识库检索",
                    content=f"知识库检索完成，命中 {len(doc_metadata)} 个片段",
                    duration_ms=kb_search_duration,
                    metadata=retrieve_metadata,
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
