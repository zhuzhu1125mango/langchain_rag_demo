"""语义缓存阶段：命中回放与写入（CODE-1 拆分自 stages）。"""

import asyncio
import logging

from src.config import settings
from src.services.decision_pipeline import QAMode
from src.services.pipeline.state import (
    _PipelineState,
    _background_store_tasks,
)
from src.services.reasoning import (
    REASONING_STEP_CACHE_HIT,
)
from src.services.semantic_cache_service import SemanticCacheService
from src.utils.log_context import log_stage

logger = logging.getLogger("rag_system")

class CacheStagesMixin:
    """语义缓存查找/回放/写入，由 PipelineStagesMixin 组合。"""

    @log_stage("semantic_cache")
    async def _stage_semantic_cache_lookup(self, state: _PipelineState):
        """阶段 5.5：语义缓存查找（P1-3）。命中时回放缓存答案并短路后续流程。

        仅当 KB 决策判定走纯知识库问答路径（PURE_KB / HYBRID_INTELLIGENT）、
        未开启联网搜索、deep_thinking=off 且非时效性问题时尝试查找。
        Redis/Embedding 异常由服务内部 fail-open，返回 miss 继续正常流程。
        """
        if not settings.semantic_cache.SEMANTIC_CACHE_ENABLED:
            return
        decision = state.decision
        if decision is None or decision.mode not in (QAMode.PURE_KB, QAMode.HYBRID_INTELLIGENT):
            return
        if not state.kb_ids or state.use_web_search or state.deep_thinking == "on":
            return
        if state.intent_decision is not None and state.intent_decision.needs_realtime:
            return

        service = SemanticCacheService()
        hit, embedding = await service.lookup(
            user_id=state.user_id or "anonymous",
            kb_ids=state.kb_ids,
            question=state.resolved_question,
        )
        # 未命中：缓存 embedding 供 finalize 后写入复用，避免二次计算
        state.semantic_cache_embedding = embedding
        if hit is None:
            return

        async for ev in self._replay_cache_hit(state, hit):
            yield ev

    async def _replay_cache_hit(self, state: _PipelineState, hit):
        """回放缓存命中：发出 reasoning + 分片答案事件并置 finished（前端零改动）。

        供精确匹配（阶段 1.5 前置）与语义匹配（阶段 5.5）两处共用。
        """
        state.semantic_cache_hit = True
        state.trace.data["semantic_cache_hit"] = {
            "score": round(hit.score, 4),
            "match_type": hit.match_type,
            "cached_question": hit.question,
        }
        if hit.match_type == "exact":
            content = "命中相同问题缓存（完全匹配），直接返回缓存答案"
        else:
            content = f"命中相似问题缓存（相似度 {hit.score:.2f}），直接返回缓存答案"
        yield ("reasoning", self._reasoning_payload(
            REASONING_STEP_CACHE_HIT,
            "done",
            "缓存命中",
            content=content,
            metadata={"score": round(hit.score, 4), "match_type": hit.match_type},
        ))

        # 回放缓存答案与来源（事件结构与正常流式一致，前端零改动）
        state.source_texts = list(hit.source_texts)
        state.source_metadata = list(hit.source_metadata)
        state.answer_type = "knowledge_base"
        chunk_chars = max(1, settings.semantic_cache.SEMANTIC_CACHE_REPLAY_CHUNK_CHARS)
        answer = hit.answer or ""
        for i in range(0, len(answer), chunk_chars):
            part = answer[i:i + chunk_chars]
            state.final_answer += part
            yield ("chunk", part, state.source_texts, state.source_metadata, "knowledge_base")
        state.finished = True

    def _maybe_store_semantic_cache(self, state: _PipelineState):
        """finalize 时按条件把纯知识库答案异步写入语义缓存（不阻塞响应）。"""
        if state.semantic_cache_hit or not settings.semantic_cache.SEMANTIC_CACHE_ENABLED:
            return
        if state.answer_type != "knowledge_base" or not state.final_answer:
            return
        if state.web_sources_for_citation or state.search_context:
            return
        if state.decision is None or state.decision.mode not in (QAMode.PURE_KB, QAMode.HYBRID_INTELLIGENT):
            return
        if not state.kb_ids or not state.docs or state.deep_thinking == "on":
            return
        if state.intent_decision is not None and state.intent_decision.needs_realtime:
            return

        service = SemanticCacheService()
        task = asyncio.create_task(service.store(
            user_id=state.user_id or "anonymous",
            kb_ids=state.kb_ids,
            question=state.resolved_question,
            answer=state.final_answer,
            source_texts=state.source_texts,
            source_metadata=state.source_metadata,
            embedding=state.semantic_cache_embedding,
        ))
        # 防止任务被垃圾回收（fire-and-forget）
        _background_store_tasks.add(task)
        task.add_done_callback(_background_store_tasks.discard)
