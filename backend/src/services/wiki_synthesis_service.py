"""答案回流服务（阶段一 D3，见 docs/design/wiki-navigable-workspace.md §4）。

把「已通过引用补全/事实校验的纯知识库答案」经去抖批量沉淀为 wiki synthesis
页（Karpathy Pattern 的 query 工作流回流：有价值的答案成为可复用知识）。

关键设计：
- 触发判定为纯函数 should_synthesize（单测友好）；
- 语义去重：问题向量与既有 synthesis 页检索相似度 ≥ 0.92 时跳过（fail-open）；
- per-KB 去抖缓冲（复用 WIKI_COMPILE_DEBOUNCE_SECONDS），窗口内多条问答
  合并成多次页面合成，避免逐条 LLM 调用放大；
- 持久化复用 WikiCompiler.persist_pages（MinIO 正文 + wiki_pages upsert +
  links 提取 + 向量入库 source_kind="wiki_syn" + 索引页维护）；
- 全链路 best-effort：任何异常只记日志，绝不阻断聊天主流程。
"""

import asyncio
import logging
from typing import Dict, List, Optional

from src.config import settings

logger = logging.getLogger("rag_system")

# 与语义缓存一致的相似度阈值：已有足够相近的综合页则不再重复沉淀
DEDUPE_SCORE_THRESHOLD = 0.92


def should_synthesize(
    answer_type: str,
    final_answer: str,
    source_metadata: List[Dict],
    has_web_context: bool,
    min_chars: Optional[int] = None,
) -> bool:
    """纯函数判定：本次答案是否值得回流为 synthesis 页（不含 enabled 开关）。

    条件：
    - 仅纯知识库 / 混合检索答案（联网、工具、Agent 纯模式不回流）；
    - 答案长度 ≥ WIKI_SYNTHESIS_MIN_ANSWER_CHARS；
    - 引用 ≥2 个不同源文档，或引用中含 wiki 编译页（说明走了综合知识）。
    """
    if answer_type not in ("knowledge_base", "hybrid_search"):
        return False
    if has_web_context:
        return False
    threshold = (
        settings.wiki_compile.WIKI_SYNTHESIS_MIN_ANSWER_CHARS
        if min_chars is None
        else min_chars
    )
    if len(final_answer or "") < threshold:
        return False
    doc_ids = {
        str(m.get("document_id", ""))
        for m in (source_metadata or [])
        if m.get("document_id")
    }
    has_wiki = any(m.get("source_kind") == "wiki" for m in (source_metadata or []))
    return len(doc_ids) >= 2 or has_wiki


class WikiSynthesisService:
    """per-KB 去抖缓冲 + synthesis 页合成与持久化（best-effort）。"""

    def __init__(self, compiler=None, debounce_seconds: Optional[int] = None):
        self._compiler = compiler
        self._debounce = debounce_seconds
        self._buffers: Dict[str, List[Dict]] = {}
        self._tasks: Dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # 依赖
    # ------------------------------------------------------------------
    def _get_compiler(self):
        if self._compiler is None:
            from src.services.wiki_compiler import WikiCompiler

            self._compiler = WikiCompiler()
        return self._compiler

    def _debounce_seconds(self) -> int:
        if self._debounce is not None:
            return self._debounce
        return max(0, settings.wiki_compile.WIKI_COMPILE_DEBOUNCE_SECONDS)

    # ------------------------------------------------------------------
    # 调度入口
    # ------------------------------------------------------------------
    async def schedule(
        self,
        kb_ids: List[str],
        question: str,
        answer: str,
        source_texts: List[str],
        source_metadata: List[Dict],
    ) -> None:
        """缓冲一次问答（语义去重后），按 KB 去抖窗口批量合成。"""
        if not kb_ids or not question or not answer:
            return
        if await self._similar_synthesis_exists(kb_ids, question):
            logger.info("synthesis 回流跳过：已存在足够相近的综合页")
            return
        doc_ids = sorted({
            str(m.get("document_id", ""))
            for m in (source_metadata or [])
            if m.get("document_id")
        })
        item = {
            "question": question,
            "answer": answer,
            "source_texts": list(source_texts or [])[:6],
            "doc_ids": doc_ids,
        }
        for kb_id in {str(k) for k in kb_ids}:
            self._buffers.setdefault(kb_id, []).append(dict(item, kb_id=kb_id))
            self._arm_timer(kb_id)

    async def _similar_synthesis_exists(self, kb_ids: List[str], question: str) -> bool:
        try:
            from src.services.kb_retrieval_service import KBRetrievalService

            service = KBRetrievalService()
            docs = await service.retrieve(
                question, kb_ids=kb_ids, source_kind="wiki_syn"
            )
            for d in docs or []:
                meta = getattr(d, "metadata", None) or {}
                score = meta.get("score") or 0.0
                try:
                    if float(score) >= DEDUPE_SCORE_THRESHOLD:
                        return True
                except (TypeError, ValueError):
                    continue
        except Exception as e:
            logger.warning(f"synthesis 去重检索失败（按不存在处理）: {e}")
        return False

    # ------------------------------------------------------------------
    # 去抖与刷写
    # ------------------------------------------------------------------
    def _arm_timer(self, kb_id: str) -> None:
        old = self._tasks.pop(kb_id, None)
        if old is not None and not old.done():
            old.cancel()
        self._tasks[kb_id] = asyncio.create_task(
            self._flush_later(kb_id, self._debounce_seconds())
        )

    async def _flush_later(self, kb_id: str, delay: int) -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        self._tasks.pop(kb_id, None)
        items = self._buffers.pop(kb_id, [])
        if items:
            await self.flush(kb_id, items)

    async def flush(self, kb_id: str, items: List[Dict]) -> None:
        """把缓冲的问答合成为 synthesis 页并持久化（单 KB，顺序执行）。"""
        compiler = self._get_compiler()
        try:
            from src.database import async_session_maker

            async with async_session_maker() as db:
                existing_titles = await self._existing_titles(db, kb_id)
                for item in items:
                    page = await compiler.compile_synthesis_page(
                        question=item["question"],
                        answer=item["answer"],
                        cited_texts=item.get("source_texts") or [],
                        existing_titles=existing_titles,
                    )
                    if page is None:
                        continue
                    result = await compiler.persist_pages(
                        db, kb_id, item.get("doc_ids") or [], [page]
                    )
                    # 更新既有标题集合，供后续条目互链
                    existing_titles.append(page.title)
                    logger.info(
                        f"synthesis 页已沉淀: {page.title} "
                        f"(created={result.pages_created}, updated={result.pages_updated})"
                    )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"synthesis 回流写入失败（忽略）: {e}")

    async def _existing_titles(self, db, kb_id: str) -> List[str]:
        from sqlalchemy import select

        from src.models.wiki_page import WikiPage

        rows = (
            await db.execute(
                select(WikiPage.title).filter(
                    WikiPage.kb_id == str(kb_id),
                    WikiPage.status == "active",
                )
            )
        ).scalars().all()
        return [str(t) for t in rows]


# 进程级单例（缓冲与去抖任务挂在其上）
wiki_synthesis_service = WikiSynthesisService()


async def maybe_schedule_synthesis(state) -> None:
    """管线终态钩子：按条件把本次问答交给回流服务（内部全量容错）。"""
    if not settings.wiki_compile.WIKI_SYNTHESIS_ENABLED:
        return
    if not state.kb_ids:
        return
    if not should_synthesize(
        answer_type=state.answer_type,
        final_answer=state.final_answer,
        source_metadata=state.source_metadata or [],
        has_web_context=bool(state.web_sources_for_citation or state.search_context),
    ):
        return
    await wiki_synthesis_service.schedule(
        kb_ids=state.kb_ids,
        question=state.resolved_question,
        answer=state.final_answer,
        source_texts=state.source_texts or [],
        source_metadata=state.source_metadata or [],
    )
