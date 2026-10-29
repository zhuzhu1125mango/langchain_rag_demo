"""LLM-Wiki 编译器（CODE-1 拆分后为组合类）。"""

import logging
from typing import List, Optional, Sequence, Union

from src.config import settings
from src.services.wiki_lock import kb_wiki_lock as _kb_wiki_lock

from src.services.wiki_pages import PageCompileMixin
from src.services.wiki_persist import PagePersistMixin
from src.services.wiki_text import (  # noqa: F401  兼容历史导入路径
    _cosine,
    _parse_json_dict,
    _split_sentences,
    _strip_think,
    build_material,
    derive_links_from_titles,
    extract_links,
    normalize_doc_ids,
    normalize_title,
)
from src.utils.error_handling import ErrorSeverity, log_exception
from src.services.wiki_types import (  # noqa: F401  兼容历史导入路径
    _THINK_RE,
    _WIKI_LINK_RE,
    COMPILE_MAX_OUTPUT_TOKENS,
    FACT_RETENTION_TARGET,
    FACT_RETENTION_THRESHOLD,
    FACT_RETENTION_WARN,
    MAX_MATERIAL_CHARS,
    TITLE_MATCH_THRESHOLD,
    CompiledPage,
    ExistingPage,
    WikiCompileResult,
)

logger = logging.getLogger("wiki_compiler")

class WikiCompiler(PageCompileMixin, PagePersistMixin):
    """单知识库 Wiki 编译器（无状态，方法内自持 LLM/Embedding 懒加载）。"""

    def __init__(self):
        self._llm = None
        self._embeddings = None
        self._model_generation: Optional[str] = None

    # ------------------------------------------------------------------
    # 模型懒加载
    # ------------------------------------------------------------------

    async def _get_llm(self):
        from src.services.model_manager import model_manager

        generation = model_manager.registry_generation
        if self._llm is None or self._model_generation != generation:
            # 编译走 registry 的 wiki_compile 路由；legacy 默认仍为 fast 模型。
            self._llm = await model_manager.get_chat_llm(
                "wiki_compile",
                streaming=False,
                think=False,
                temperature=0,
                max_output_tokens=COMPILE_MAX_OUTPUT_TOKENS,
            )
            self._model_generation = generation
        return self._llm

    async def _ainvoke(self, prompt: str):
        import inspect

        llm = self._get_llm()
        if inspect.isawaitable(llm):
            llm = await llm
        return await llm.ainvoke(prompt)

    def _get_embeddings(self):
        # 复用 model_manager 共享 OllamaEmbeddings 单例（B2，与检索链路同源连接），
        # 避免自建实例在容器环境因缺少连接配置而失效；P3 模型切换后自动刷新
        from src.services.model_manager import model_manager

        try:
            current_key = model_manager.embedding_model_key
        except Exception:
            # 仅用于判断缓存是否失效：取不到就当未知，下次照常复用实例
            current_key = ""
        if self._embeddings is None or (current_key and current_key != getattr(self, "_embeddings_key", "")):
            self._embeddings = model_manager.get_embeddings()
            self._embeddings_key = current_key
        return self._embeddings

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    async def compile_document(
        self,
        db,
        kb_id: str,
        doc_ids: Union[str, Sequence[str]],
        chunks,
    ) -> WikiCompileResult:
        """编排：加载既有页 → LLM 编译 → 探针+迭代精炼（内存态）→ 矛盾抽查 → 持久化。

        doc_ids 支持单值 str（兼容单文档场景）或多值（P5 去抖合并编译），
        统一归一化为列表后用于 source_doc_ids 归属。同一 KB 内串行
        （kb_wiki_lock 双层锁，等待时间计入外层 WIKI_COMPILE_TIMEOUT_SECONDS）。
        探针/精炼/抽查均为 persist 前的内存态操作，不产生中间向量；失败不阻断持久化。
        """
        doc_id_list = normalize_doc_ids(doc_ids)
        async with _kb_wiki_lock(kb_id):
            existing = await self._load_existing_pages(db, kb_id)
            compiled = await self.compile_pages(chunks, existing)
            if not compiled:
                return WikiCompileResult()

            # P4：诊断探针 + 迭代精炼（REFINEMENT_ITERATIONS=0 且探针关闭时整体跳过，
            # 行为与 Phase 1/2 完全一致）
            probe_map = None
            if (
                settings.wiki_compile.WIKI_COMPILE_REFINEMENT_ITERATIONS > 0
                or settings.wiki_compile.WIKI_DIAGNOSTIC_PROBES
            ):
                probe_map = await self._refinement_loop(compiled)

            # P5：矛盾抽查（仅诊断，不改页面内容；persist 前内存态执行）
            if settings.wiki_compile.WIKI_CONTRADICTION_CHECK:
                await self._check_contradictions(
                    compiled, build_material(chunks, MAX_MATERIAL_CHARS)
                )

            result = await self.persist_pages(db, kb_id, doc_id_list, compiled)

        result.fact_retention_rate = self._aggregate_retention(probe_map, compiled)
        return result

    async def _load_existing_pages(self, db, kb_id: str) -> List[ExistingPage]:
        """加载该 KB 现有实体/主题页（含 MinIO 正文，读取失败按空内容处理）。"""
        from sqlalchemy import select

        from src.models.wiki_page import WikiPage
        from src.services.minio_service import MinioService

        try:
            result = await db.execute(
                select(WikiPage).filter(
                    WikiPage.kb_id == str(kb_id),
                    WikiPage.status == "active",
                    WikiPage.page_type.in_(("entity", "topic")),
                )
            )
            rows = result.scalars().all()
        except Exception as e:
            # 返回空列表会让全部页面按「新建」编译（revision 归零、旧页不合并）。
            # 即便底层是连接类异常（自动分级会判可恢复），后果也是数据一致性受损，
            # 因此显式抬到需上报：这条一旦出现，本次编译产物需要人工核对。
            log_exception(
                logger, e,
                action="wiki.compile.load_existing_pages",
                severity=ErrorSeverity.REPORTABLE,
                message="加载既有 Wiki 页失败，本次按空白基线编译（产物需人工核对）",
                kb_id=str(kb_id),
            )
            return []

        if not rows:
            return []

        minio = await MinioService.get_instance()
        pages = []
        for row in rows:
            content = await minio.download_text_async(row.content_path)
            pages.append(
                ExistingPage(
                    page_id=row.id,
                    title=row.title,
                    page_type=row.page_type,
                    content=content or "",
                )
            )
        return pages

    # ------------------------------------------------------------------
    # 纯编译层（LLM only，供单测/A/B 评估独立调用）
    # ------------------------------------------------------------------

    async def compile_pages(self, chunks, existing_pages: List[ExistingPage]) -> List[CompiledPage]:
        """从 chunks 抽取候选页，与既有页匹配后生成/增量合并页面内容。"""
        material = build_material(chunks, MAX_MATERIAL_CHARS)
        if not material.strip():
            return []

        candidates = await self._extract_candidates(material)
        if not candidates:
            return []

        cap = settings.wiki_compile.WIKI_COMPILE_MAX_PAGES_PER_DOC
        candidates = candidates[:cap]

        matched = await self._match_pages(candidates, existing_pages)

        compiled: List[CompiledPage] = []
        for cand, existing in zip(candidates, matched):
            try:
                content = await self._generate_page(cand, existing, material)
            except Exception as e:
                log_exception(
                    logger, e,
                    action="wiki.compile.generate_page",
                    severity=ErrorSeverity.RECOVERABLE,
                    message="Wiki 页生成失败，跳过该页",
                    page=cand.get("title"),
                )
                continue
            compiled.append(
                CompiledPage(
                    title=cand["title"],
                    page_type=cand.get("type", "topic"),
                    content=content,
                    matched=existing,
                    key_facts=cand.get("key_facts", []),
                )
            )
        return compiled

__all__ = [
    "WikiCompiler",
    "CompiledPage",
    "ExistingPage",
    "WikiCompileResult",
    "build_material",
    "normalize_doc_ids",
    "extract_links",
    "normalize_title",
    "derive_links_from_titles",
    "_cosine",
    "_parse_json_dict",
    "_split_sentences",
    "_strip_think",
    "_THINK_RE",
    "_WIKI_LINK_RE",
    "TITLE_MATCH_THRESHOLD",
    "MAX_MATERIAL_CHARS",
    "FACT_RETENTION_THRESHOLD",
    "FACT_RETENTION_WARN",
    "FACT_RETENTION_TARGET",
    "COMPILE_MAX_OUTPUT_TOKENS",
]
