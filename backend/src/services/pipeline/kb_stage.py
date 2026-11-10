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

from src.utils.error_handling import ErrorSeverity, log_exception


def summarize_gate_suppression(docs, threshold: float) -> dict:
    """把「门禁清空上下文」整理成可核对的事实：分数多高、差阈值多少、丢掉的是哪块。

    为什么单独抽出来：这段判定此前是**静默**的 —— 检索到了片段、分数没够，
    直接把 `state.docs` 清空，用户看到「知识库里没有找到相关内容」，
    运维侧没有任何痕迹。2026-11-10 的离线评估（`scripts/eval_relevance_gate.py`）
    显示这种误判真实存在：24 题里 2 道「能答」的题被拒，只拦下 1 道真该拒的。
    要判断该不该动阈值，先得能量出「生产上到底拒了多少、拒的是什么」，
    所以这里必须把那一块的分数与开头文本落进日志。
    """
    scores = [float(getattr(d, "metadata", {}).get("rerank_score", 0.0) or 0.0) for d in docs]
    max_score = max(scores) if scores else 0.0
    first = docs[0] if docs else None
    return {
        "threshold": threshold,
        "chunks": len(docs),
        "max_score": round(max_score, 4),
        "avg_score": round(sum(scores) / len(scores), 4) if scores else 0.0,
        "gap_to_threshold": round(threshold - max_score, 4),
        "top1_preview": ((getattr(first, "page_content", "") or "")[:80].replace("\n", " ")
                         if first is not None else ""),
    }


def log_gate_suppression(docs, threshold: float) -> dict:
    """记录一次「门禁清空上下文」，返回落进日志的字段（便于单测断言）。

    单独成一个函数而不是内联：这条日志是判断「阈值该不该动」的唯一生产证据，
    必须能被测试直接调用到，而不是只测一个拼好的 dict。
    """
    payload = summarize_gate_suppression(docs, threshold)
    log_exception(
        logger,
        RuntimeError("KB relevance gate suppressed all retrieved chunks"),
        action="pipeline.kb.relevance_gate_suppressed",
        severity=ErrorSeverity.REPORTABLE,
        message="检索到片段但均低于相关性阈值，已清空上下文并按「未找到」回答",
        # 这是设计内的业务事件，不是捕获到的异常：不要打一行假的堆栈
        include_traceback=False,
        **payload,
    )
    return payload


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

        # 标记「本次确实检索过」：即使下面判为不相关、docs 被清空，
        # 生成阶段也要据此说「知识库里没有」，而不是切到纯聊天模式去编答案。
        state.kb_retrieval_attempted = True

        # C11：检索结果必须落 trace，且**必须在相关性判定之前**记录。
        # 判定为「不相关」时下面会把 state.docs 清空，若在清空后才写，
        # 恰好丢掉最需要复盘的那批题：看 trace 只见空结果，分不清
        # 「没检索到」和「检索到但被判定不相关」——两者的修法完全不同。
        # 只记候选与分数，不改变主链路行为；可观测性失败不能拖垮问答。
        if state.trace is not None:
            try:
                state.trace.set_kb_results(state.docs)
            except Exception as exc:  # noqa: BLE001 - best-effort
                log_exception(
                    logger,
                    exc,
                    action="pipeline.kb.trace_kb_results",
                    message="检索结果写入 trace 失败（不影响问答）",
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
                # 判定「不相关」= 把已经检索到的片段全部丢掉。用户会看到
                # 「知识库里没有找到相关内容」，但库里可能真有答案 —— 区别只在
                # 重排给了个低分。这条日志是唯一能事后核对的证据：
                # 没有它，「拒了多少、拒掉的是不是本该能答」永远无法回答。
                log_gate_suppression(
                    state.docs, settings.processing.KB_RELEVANCE_SCORE_THRESHOLD
                )
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
