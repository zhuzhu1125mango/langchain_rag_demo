"""终态阶段：副作用收尾与 finalize（CODE-1 拆分自 stages）。"""

import asyncio
import logging

from src.services.pipeline.state import (
    _PipelineState,
    _background_store_tasks,
    _request_wiki_nav,
    summarize_wiki_nav,
)
from src.utils.log_context import log_stage

logger = logging.getLogger("rag_system")

from src.utils.error_handling import log_exception

class FinalizeStagesMixin:
    """终态清理与副作用，由 PipelineStagesMixin 组合。"""

    async def _finalize_side_effects(self, state: _PipelineState):
        """终态清理副作用：语义缓存写入 + 链路落盘 + 对话摘要更新。

        与流式 ("final", ...) 事件解耦，使 SSE 客户端中途断连（生成器被
        aclose 关闭而中断于某条 yield）时，这些持久化操作仍会执行，而非
        连同 final 事件一起被丢弃。幂等：state.finalized 防重复执行。
        """
        if state.finalized:
            return
        state.finalized = True
        try:
            # P1-3：符合条件的纯知识库答案异步写入语义缓存
            self._maybe_store_semantic_cache(state)
            # 阶段一 D3：答案回流为 wiki synthesis 页（WIKI_SYNTHESIS_ENABLED 灰度，
            # 内部去抖+best-effort，绝不阻断响应）
            try:
                from src.services.wiki_synthesis_service import maybe_schedule_synthesis

                await maybe_schedule_synthesis(state)
            except Exception as exc:
                log_exception(
                    logger,
                    exc,
                    action="pipeline.synthesis.schedule",
                    message="synthesis 回流调度失败（忽略）",
                )
            # E-1 W3：wiki_lookup 导航埋点汇总写入 trace（存 stages 明细，规避 trace 表未知列报错）
            _wiki_nav = summarize_wiki_nav(_request_wiki_nav.get())
            if _wiki_nav:
                state.trace.add_stage("wiki_nav", "done", latency_ms=0, detail=_wiki_nav)
            state.trace.set_final_answer(state.final_answer)
            state.trace.finish()
            state.trace.save_background()
            # B 尾部阻塞消除：对话摘要更新是一次完整 LLM 调用，此前直接 await
            # 会阻塞 ("final", ...) 事件（答案早已流完，用户还在转圈）。改为
            # fire-and-forget 后台任务（同 _maybe_store_semantic_cache 范式）。
            summary_task = asyncio.create_task(
                self._update_conversation_summary(state.history or [], state.session_id)
            )
            _background_store_tasks.add(summary_task)
            summary_task.add_done_callback(_background_store_tasks.discard)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 收尾为 best-effort：异常不回传客户端、不阻塞断连；但需可判定——
            # 网络/超时类记 warning，其余（含编程错误）记 error 并附堆栈
            log_exception(logger, exc, action="pipeline.finalize", message="终态清理异常")

    @log_stage("finalize")

    @log_stage("finalize")
    async def _finalize(self, state: _PipelineState):
        """终态：执行清理副作用并产出 ("final", state) 事件。"""
        await self._finalize_side_effects(state)
        yield ("final", state)
