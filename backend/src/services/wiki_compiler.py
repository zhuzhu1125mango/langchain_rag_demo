"""LLM-Wiki 编译器（P2 前置编译增强，Phase 1 MVP）。

将新摄入文档的 chunks「编译」为该知识库的 Wiki 页面（实体页/主题页），
作为补充语料入向量库参与混合检索（source_kind="wiki"）。raw 层不可变，
编译失败不影响原始检索链路。

结构分两层便于单测与 A/B 评估：
- compile_pages：纯 LLM 编译（抽取 → 页面匹配 → 生成/增量合并），无 IO 依赖
- persist_pages：MinIO 正文持久化 + wiki_pages 表 upsert + 向量入库 + 索引页维护
"""

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Union

from src.config import settings
from src.services.wiki_lock import kb_wiki_lock as _kb_wiki_lock

logger = logging.getLogger("wiki_compiler")

# 标题匹配的 embedding 余弦阈值（低于则视为新页）
TITLE_MATCH_THRESHOLD = 0.85
# 送入 LLM 的来源材料上限（字符），防止超出本地模型上下文
MAX_MATERIAL_CHARS = 8000
# 诊断探针：key_fact 与页面句子的余弦达到该值视为「事实保留」
FACT_RETENTION_THRESHOLD = 0.75
# 诊断探针：事实保留率低于该值记 warning（对齐评估上线门槛 0.9）
FACT_RETENTION_WARN = 0.9
# P4 迭代精炼：页面事实保留率低于该值视为弱页，触发重生成
FACT_RETENTION_TARGET = 0.9
# 候选抽取的 JSON 提取兜底正则（容忍模型输出包裹文字/代码围栏）
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


@dataclass
class ExistingPage:
    """既有 Wiki 页（编译时用于标题匹配与增量合并）。"""

    page_id: str
    title: str
    page_type: str
    content: str


@dataclass
class CompiledPage:
    """编译产出的页面（待持久化）。"""

    title: str
    page_type: str  # entity / topic
    content: str
    # 命中的既有页（None = 新建页）
    matched: Optional[ExistingPage] = None
    key_facts: List[str] = field(default_factory=list)


@dataclass
class WikiCompileResult:
    pages_created: int = 0
    pages_updated: int = 0
    chunks_indexed: int = 0
    # 诊断探针（WIKI_DIAGNOSTIC_PROBES 开启时）：key_facts 事实保留率，None=未探测
    fact_retention_rate: Optional[float] = None


class WikiCompiler:
    """单知识库 Wiki 编译器（无状态，方法内自持 LLM/Embedding 懒加载）。"""

    def __init__(self):
        self._llm = None
        self._embeddings = None

    # ------------------------------------------------------------------
    # 模型懒加载
    # ------------------------------------------------------------------
    def _get_llm(self):
        if self._llm is None:
            from langchain_ollama import ChatOllama

            model_name = (
                settings.wiki_compile.WIKI_COMPILE_MODEL
                or settings.model.OLLAMA_MODEL_NAME
            )
            self._llm = ChatOllama(
                model=model_name,
                streaming=False,
                num_ctx=settings.model.OLLAMA_NUM_CTX,
                temperature=0,
            )
        return self._llm

    def _get_embeddings(self):
        # 复用 model_manager 共享 OllamaEmbeddings 单例（B2，与检索链路同源连接），
        # 避免自建实例在容器环境因缺少连接配置而失效
        if self._embeddings is None:
            from src.services.model_manager import model_manager

            self._embeddings = model_manager.get_embeddings()
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
            logger.warning(f"加载既有 Wiki 页失败，按空白编译处理: {e}")
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
                logger.warning(f"Wiki 页「{cand['title']}」生成失败，跳过: {e}")
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

    async def _extract_candidates(self, material: str) -> List[dict]:
        """LLM 抽取候选实体/主题页，返回 [{"title","type","key_facts"}]；解析失败返回空。"""
        prompt = (
            "你是知识库编译助手。从以下文档内容中提取值得建立独立 Wiki 页面的"
            "实体（产品/系统/概念/人物/组织）与主题（跨章节议题）。\n"
            "要求：\n"
            "- 只提取文档中有实质内容支撑的条目，最多 8 个\n"
            "- title 为简短名词（不超过 20 字），type 取 entity 或 topic\n"
            "- key_facts 为该条目下用一句话陈述的关键事实\n\n"
            f"文档内容：\n{material}\n\n"
            '严格输出 JSON，不要输出任何其他文字：\n'
            '{"pages": [{"title": "...", "type": "entity", "key_facts": ["..."]}]}'
        )
        pages: list = []
        for attempt in range(2):
            # LLM JSON 输出偶发畸变（截断/未转义引号），失败重试一次；
            # 仍失败才放弃（该文档零页面，仅损失编译覆盖，不阻断上传管线）
            try:
                resp = await self._get_llm().ainvoke(prompt)
                pages = _parse_json_dict(_strip_think(resp.content)).get("pages", [])
                break
            except Exception as e:
                if attempt == 1:
                    logger.warning(f"Wiki 候选抽取失败: {e}")

        valid = []
        for p in pages if isinstance(pages, list) else []:
            title = str(p.get("title", "")).strip()
            if not title:
                continue
            valid.append(
                {
                    "title": title[:256],
                    "type": p.get("type") if p.get("type") in ("entity", "topic") else "topic",
                    "key_facts": [str(f) for f in (p.get("key_facts") or [])][:10],
                }
            )
        return valid

    async def _match_pages(
        self, candidates: List[dict], existing_pages: List[ExistingPage]
    ) -> List[Optional[ExistingPage]]:
        """候选页与既有页匹配：归一化标题精确命中优先，其余按 embedding 余弦。

        embedding 失败时降级为仅精确匹配（未匹配一律按新页处理）。
        """
        matched: List[Optional[ExistingPage]] = []
        remaining = list(existing_pages)
        pending_idx: List[int] = []

        for i, cand in enumerate(candidates):
            norm = normalize_title(cand["title"])
            hit = next((p for p in remaining if normalize_title(p.title) == norm), None)
            if hit:
                matched.append(hit)
                remaining.remove(hit)
            else:
                matched.append(None)
                pending_idx.append(i)

        if pending_idx and remaining:
            try:
                matched = await self._match_by_embedding(
                    candidates, pending_idx, remaining, matched
                )
            except Exception as e:
                logger.warning(f"Wiki 页标题 embedding 匹配失败，按新页处理: {e}")
        return matched

    async def _match_by_embedding(
        self,
        candidates: List[dict],
        pending_idx: List[int],
        remaining: List[ExistingPage],
        matched: List[Optional[ExistingPage]],
    ) -> List[Optional[ExistingPage]]:
        """对未精确命中的候选用 embedding 余弦匹配余下既有页（贪心取最高分）。"""
        texts = [candidates[i]["title"] for i in pending_idx] + [p.title for p in remaining]
        vectors = await self._get_embeddings().aembed_documents(texts)
        cand_vecs = vectors[: len(pending_idx)]
        page_vecs = vectors[len(pending_idx):]

        for pos, i in enumerate(pending_idx):
            best_score, best = 0.0, None
            for j, page in enumerate(list(remaining)):
                score = _cosine(cand_vecs[pos], page_vecs[j])
                if score > best_score:
                    best_score, best = score, page
            if best is not None and best_score >= TITLE_MATCH_THRESHOLD:
                matched[i] = best
                remaining.remove(best)
        return matched

    async def _generate_page(
        self, cand: dict, existing: Optional[ExistingPage], material: str
    ) -> str:
        """生成新页或增量合并既有页（约束：保留既有事实，矛盾显式标注）。"""
        title = cand["title"]
        facts = "\n".join(f"- {f}" for f in cand.get("key_facts", [])) or "（无）"
        if existing is not None:
            prompt = (
                "你是 Wiki 编辑。请基于既有页面与新增材料，输出更新后的完整 Markdown 页面。\n"
                "规则：\n"
                "- 保留既有页面中的全部事实，不得删除\n"
                "- 用新增材料补充新事实、修正过时表述\n"
                "- 若新旧信息矛盾，用引用块「> ⚠️ 矛盾提示：...」显式标注，不要静默覆盖\n"
                "- 只使用给定材料中的信息，不要编造；输出正文，不要解释\n\n"
                f"页面标题：{title}\n\n"
                f"既有页面内容：\n{existing.content or '（空）'}\n\n"
                f"新增材料关键事实：\n{facts}\n\n"
                f"新增材料原文（节选）：\n{material}\n"
            )
        else:
            prompt = (
                "你是 Wiki 编辑。请根据来源材料为指定条目编写一个 Markdown Wiki 页面。\n"
                "规则：\n"
                "- 以「# 标题」开头，分小节陈述事实\n"
                "- 只使用来源材料中的信息，不要编造\n"
                "- 末尾加「## 来源」小节，说明信息来自本次编译的文档\n\n"
                f"页面标题：{title}\n\n"
                f"关键事实：\n{facts}\n\n"
                f"来源材料（节选）：\n{material}\n"
            )
        resp = await self._get_llm().ainvoke(prompt)
        content = _strip_think(resp.content).strip()
        return content or f"# {title}\n\n（内容生成失败）"

    # ------------------------------------------------------------------
    # 持久化层
    # ------------------------------------------------------------------
    async def persist_pages(
        self,
        db,
        kb_id: str,
        doc_ids: Union[str, Sequence[str]],
        compiled: List[CompiledPage],
    ) -> WikiCompileResult:
        """MinIO 正文 + wiki_pages upsert + 向量入库 + 索引页维护。

        doc_ids 支持单值 str（兼容）或多值（P5 去抖合并编译），source_doc_ids 归属全部。
        """
        doc_id_list = normalize_doc_ids(doc_ids)

        from src.models.wiki_page import WikiPage
        from src.services.minio_service import MinioService

        minio = await MinioService.get_instance()
        result = WikiCompileResult()

        owner_id = await self._get_kb_owner(db, kb_id)
        upserted_rows: List[WikiPage] = []
        was_existing: List[bool] = []

        for page in compiled:
            # 既有页按 (kb_id, page_type, title) 幂等查找，未命中则新建
            from sqlalchemy import select

            row = (
                await db.execute(
                    select(WikiPage).filter(
                        WikiPage.kb_id == str(kb_id),
                        WikiPage.page_type == page.page_type,
                        WikiPage.title == page.title,
                        WikiPage.status == "active",
                    )
                )
            ).scalars().first()

            if row is None:
                is_existing = False
                row = WikiPage(
                    id=str(uuid.uuid4()),
                    kb_id=str(kb_id),
                    page_type=page.page_type,
                    title=page.title,
                    content_path=f"wiki/{kb_id}/{str(uuid.uuid4())}.md",
                    # 显式初始化（Column default 仅在 INSERT 时生效，内存对象读不到）
                    source_doc_ids=list(doc_id_list),
                    revision=1,
                    owner_id=owner_id,
                )
                db.add(row)
                result.pages_created += 1
            else:
                is_existing = True
                row.revision = (row.revision or 1) + 1
                missing_docs = [d for d in doc_id_list if d not in (row.source_doc_ids or [])]
                if missing_docs:
                    # JSONB 原位变更不触发 UPDATE，须重新赋值
                    row.source_doc_ids = [*(row.source_doc_ids or []), *missing_docs]
                result.pages_updated += 1

            await minio.upload_text_async(row.content_path, page.content)
            upserted_rows.append(row)
            was_existing.append(is_existing)

        await db.commit()
        await self._fill_links(db, kb_id, upserted_rows, [p.content for p in compiled])

        # 页面正文分块入向量库（source_kind=wiki，参与混合检索）；
        # 既有页先删旧向量再入库，避免多 revision 重复累积
        n_indexed = 0
        for row, page, is_existing in zip(upserted_rows, compiled, was_existing):
            n_indexed += await self._index_page(kb_id, row, page, delete_old=is_existing)
        result.chunks_indexed = n_indexed

        await self._upsert_index_page(db, kb_id, minio, owner_id)
        return result

    async def _fill_links(self, db, kb_id: str, rows: List, contents: List[str]) -> None:
        """P3：为本次 upsert 的页面提取正文 [[Title]] 写 links 列（校验目标页存在）。

        在页面 upsert 提交后执行，保证批次内互链可见；失败仅告警（links 缺失
        只影响检索扩展，不影响编译产物）。
        """
        from sqlalchemy import select

        from src.models.wiki_page import WikiPage

        try:
            active_titles = (
                await db.execute(
                    select(WikiPage.title).filter(
                        WikiPage.kb_id == str(kb_id),
                        WikiPage.status == "active",
                    )
                )
            ).scalars().all()
            title_set = {normalize_title(t): t for t in active_titles}
            for row, content in zip(rows, contents):
                row.links = extract_links(content, row.title, title_set)
            await db.commit()
        except Exception as e:
            logger.warning(f"Wiki 页链接提取失败（不影响编译产物）: {e}")

    async def _index_page(self, kb_id: str, row, page: CompiledPage, delete_old: bool = False) -> int:
        """将单个页面正文分块写入向量库；失败仅告警（raw 层兜底）。

        delete_old=True（既有页更新）时先删旧向量，避免多 revision 重复累积。
        """
        try:
            from langchain_core.documents import Document
            from langchain_text_splitters import RecursiveCharacterTextSplitter

            from src.services.vector_store import VectorStoreManager

            vector_store = await VectorStoreManager.get_instance()
            if delete_old:
                await vector_store.milvus_service.delete_by_document_id(row.id)
            splitter = RecursiveCharacterTextSplitter(
                chunk_size=1000,
                chunk_overlap=100,
                separators=["\n## ", "\n### ", "\n\n", "\n", "，", "。", ""],
            )
            pieces = splitter.split_text(page.content) or [page.content]
            docs = [
                Document(
                    page_content=text,
                    metadata={
                        "document_id": row.id,
                        "source": f"wiki://{page.title}",
                        "chunk_index": i,
                        "heading_path": page.title,
                        "source_kind": "wiki",
                    },
                )
                for i, text in enumerate(pieces)
            ]
            # add_documents 无返回值（insert 语义），入库数即分块数
            await vector_store.add_documents(docs, str(kb_id))
            return len(docs)
        except Exception as e:
            logger.warning(f"Wiki 页「{page.title}」向量入库失败（raw 层兜底不受影响）: {e}")
            return 0

    async def rebuild_index_page(self, db, kb_id: str) -> None:
        """公开封装：按当前 active 页重建索引页（级联删除后由 wiki_cascade 调用）。"""
        from src.services.minio_service import MinioService

        minio = await MinioService.get_instance()
        await self._upsert_index_page(db, kb_id, minio, await self._get_kb_owner(db, kb_id))

    async def _check_fact_retention(
        self, compiled: List[CompiledPage]
    ) -> Optional[Dict[str, tuple]]:
        """诊断探针（WiCER 思路）：逐页计算 key_facts 事实保留率，零额外 LLM 调用。

        每条 key_fact 与本页句子的 embedding 余弦 max ≥ FACT_RETENTION_THRESHOLD
        视为保留；探针与精炼均未启用、无 key_facts 或 embedding 失败时返回 None。

        Returns:
            {title: (该页保留率, 缺失事实列表)}；整体保留率由 _aggregate_retention 聚合。
        """
        if (
            not settings.wiki_compile.WIKI_DIAGNOSTIC_PROBES
            and settings.wiki_compile.WIKI_COMPILE_REFINEMENT_ITERATIONS <= 0
        ):
            return None
        if not any(p.key_facts for p in compiled):
            return None
        try:
            probe: Dict[str, tuple] = {}
            for page in compiled:
                if not page.key_facts:
                    continue
                sentences = _split_sentences(page.content)
                if not sentences:
                    probe[page.title] = (0.0, list(page.key_facts))
                    continue
                vectors = await self._get_embeddings().aembed_documents(
                    page.key_facts + sentences
                )
                fact_vecs, sent_vecs = vectors[: len(page.key_facts)], vectors[len(page.key_facts):]
                missing = []
                for i, fact_vec in enumerate(fact_vecs):
                    best = max((_cosine(fact_vec, s) for s in sent_vecs), default=0.0)
                    if best < FACT_RETENTION_THRESHOLD:
                        missing.append(page.key_facts[i])
                probe[page.title] = (1.0 - len(missing) / len(page.key_facts), missing)

            overall = self._aggregate_retention(probe, compiled)
            if overall is not None:
                if overall < FACT_RETENTION_WARN:
                    logger.warning(
                        f"Wiki 编译事实保留率偏低: {overall:.2f}（阈值 {FACT_RETENTION_WARN}），"
                        "建议迭代 prompt 或换模型"
                    )
                else:
                    logger.info(f"Wiki 编译事实保留率: {overall:.2f}")
            return probe
        except Exception as e:
            logger.warning(f"Wiki 诊断探针执行失败（不影响编译产物）: {e}")
            return None

    async def _refinement_loop(self, compiled: List[CompiledPage]) -> Optional[Dict[str, tuple]]:
        """P4 迭代精炼：探针定位弱页（保留率 < FACT_RETENTION_TARGET）→ 注入缺失事实重生成。

        轮次上限 WIKI_COMPILE_REFINEMENT_ITERATIONS（0 时仅探针一次，兼容仅日志语义）；
        精炼抛错时该页保留当前内容继续（结果绝不比精炼前差）；探针失败无法定位
        弱页时立即返回。返回最终一轮探针结果（供 result 记录）。

        Returns:
            最终探针结果 dict（可能为 None=探针未产出），由 compile_document 聚合。
        """
        iterations = max(0, settings.wiki_compile.WIKI_COMPILE_REFINEMENT_ITERATIONS)
        probe_map = await self._check_fact_retention(compiled)
        for _ in range(iterations):
            if not probe_map:
                return None
            weak = [
                (page, probe_map[page.title][1])
                for page in compiled
                if page.title in probe_map and probe_map[page.title][0] < FACT_RETENTION_TARGET
            ]
            if not weak:
                return probe_map
            for page, missing in weak:
                try:
                    page.content = await self.refine_pages(page, missing)
                except Exception as e:
                    logger.warning(f"Wiki 页「{page.title}」精炼失败，保留当前内容: {e}")
            probe_map = await self._check_fact_retention(compiled)
        return probe_map

    async def refine_pages(self, page: CompiledPage, missing_facts: List[str]) -> str:
        """P4 精炼单个弱页：注入缺失事实清单重生成完整正文（纯 LLM 无 IO，可单测 mock）。

        失败/空输出抛异常，由 _refinement_loop 兜底保留当前内容。
        """
        facts = "\n".join(f"- {f}" for f in missing_facts) or "（无）"
        prompt = (
            "你是 Wiki 编辑。以下页面经事实保留率诊断，发现部分关键事实未在正文中体现。\n"
            "规则：\n"
            "- 保留既有页面全部内容与结构，不得删除既有事实\n"
            "- 将缺失的关键事实自然融入正文（可新增小节）\n"
            "- 不引入既有内容与缺失事实之外的新信息，不要编造\n"
            "- 输出完整 Markdown 页面正文，不要解释\n\n"
            f"页面标题：{page.title}\n\n"
            f"当前页面内容：\n{page.content}\n\n"
            f"缺失的关键事实（须补充）：\n{facts}\n"
        )
        resp = await self._get_llm().ainvoke(prompt)
        content = _strip_think(resp.content).strip()
        if not content:
            raise ValueError("精炼输出为空")
        return content

    async def regenerate_page(self, title: str, page_type: str, material: str) -> str:
        """P5 级联重写：仅基于剩余来源材料重推导整页（纯 LLM 无 IO，可单测 mock）。

        材料按 WIKI_REWRITE_MATERIAL_CHARS 截断（重写低频，上限独立于编译放宽）；
        空输出抛异常，由级联重写任务兜底保留当前内容。
        """
        material = (material or "")[: settings.wiki_compile.WIKI_REWRITE_MATERIAL_CHARS]
        prompt = (
            "你是 Wiki 编辑。该页面此前引用的部分来源文档已被删除，"
            "请仅基于剩余来源材料重新生成本页完整 Markdown 内容。\n"
            "规则：\n"
            "- 只使用剩余来源材料中的信息，已删除来源相关的内容一律不要保留\n"
            "- 以「# 标题」开头；原有小节结构与剩余材料匹配时保持，不匹配时允许缩减小节\n"
            "- 不要编造；末尾加「## 来源」小节，说明信息来自剩余来源文档\n"
            "- 输出正文，不要解释\n\n"
            f"页面标题：{title}\n\n"
            f"剩余来源材料（节选）：\n{material}\n"
        )
        resp = await self._get_llm().ainvoke(prompt)
        content = _strip_think(resp.content).strip()
        if not content:
            raise ValueError("重写输出为空")
        return content

    async def _check_contradictions(self, compiled: List[CompiledPage], material: str) -> None:
        """P5 矛盾抽查（仅诊断）：增量更新页的新旧版本比对，warning + 指标。

        不修改页面内容（矛盾标注仍由生成期 prompt 承担）；LLM 抛错/解析失败
        视为无矛盾仅记日志；新页（matched=None）无「新旧矛盾」语义，跳过。
        """
        from src.middleware.prometheus import record_wiki_contradictions

        for page in compiled:
            if page.matched is None:
                continue
            prompt = (
                "你是 Wiki 审校。比较同一页面的更新前后两个版本，"
                "找出新增或修改的内容与既有事实之间的矛盾"
                "（数值冲突、状态冲突、时间线冲突等）。\n"
                "规则：\n"
                "- 有意补充、细化既有内容不算矛盾\n"
                "- 只报告真正的逻辑冲突，没有则输出空数组\n\n"
                "严格输出 JSON，不要输出任何其他文字：\n"
                '{"contradictions": ["矛盾描述1", "..."]}\n\n'
                f"页面标题：{page.title}\n\n"
                f"更新前（既有版本）：\n{page.matched.content or '（空）'}\n\n"
                f"更新后（当前版本）：\n{page.content}\n\n"
                f"新增材料（供参考判断，不要输出修改建议）：\n{material}\n"
            )
            try:
                resp = await self._get_llm().ainvoke(prompt)
                data = _parse_json_dict(_strip_think(resp.content))
                contradictions = [
                    str(c).strip()
                    for c in (data.get("contradictions") or [])
                    if str(c).strip()
                ]
            except Exception as e:
                logger.warning(f"Wiki 矛盾抽查失败（视为无矛盾）: 页「{page.title}」: {e}")
                continue
            if contradictions:
                record_wiki_contradictions(len(contradictions))
                for item in contradictions:
                    logger.warning(f"Wiki 矛盾抽查发现矛盾: 页「{page.title}」: {item}")

    @staticmethod
    def _aggregate_retention(
        probe_map: Optional[Dict[str, tuple]], compiled: List[CompiledPage]
    ) -> Optional[float]:
        """由逐页探针结果聚合整体事实保留率（口径与 Phase 2 一致：保留数/总数）。"""
        if not probe_map:
            return None
        retained = total = 0
        for page in compiled:
            if not page.key_facts or page.title not in probe_map:
                continue
            rate, _ = probe_map[page.title]
            total += len(page.key_facts)
            retained += round(rate * len(page.key_facts))
        return retained / total if total else None

    async def _upsert_index_page(self, db, kb_id: str, minio, owner_id: Optional[str]) -> None:
        """维护索引页（目录，不进向量库）：每次编译后按全部 active 页重建。"""
        from sqlalchemy import select

        from src.models.wiki_page import WikiPage

        try:
            # 先取/建索引行，再查全量页面构建目录（调用序稳定，便于测试对齐）
            index_row = (
                await db.execute(
                    select(WikiPage).filter(
                        WikiPage.kb_id == str(kb_id),
                        WikiPage.page_type == "index",
                    )
                )
            ).scalars().first()
            if index_row is None:
                index_row = WikiPage(
                    id=str(uuid.uuid4()),
                    kb_id=str(kb_id),
                    page_type="index",
                    title="索引",
                    content_path=f"wiki/{kb_id}/{str(uuid.uuid4())}.md",
                    source_doc_ids=[],
                    revision=1,
                    owner_id=owner_id,
                )
                db.add(index_row)
            else:
                index_row.revision = (index_row.revision or 1) + 1

            rows = (
                await db.execute(
                    select(WikiPage)
                    .filter(
                        WikiPage.kb_id == str(kb_id),
                        WikiPage.status == "active",
                        WikiPage.page_type.in_(("entity", "topic")),
                    )
                    .order_by(WikiPage.page_type, WikiPage.title)
                )
            ).scalars().all()

            lines = ["# 知识库索引", ""]
            current_type = None
            for row in rows:
                if row.page_type != current_type:
                    label = "实体" if row.page_type == "entity" else "主题"
                    lines += [f"## {label}页", ""]
                    current_type = row.page_type
                lines.append(f"- [[{row.title}]]（revision {row.revision}）")
            lines += ["", f"共 {len(rows)} 个页面。"]
            content = "\n".join(lines)

            await minio.upload_text_async(index_row.content_path, content)
            await db.commit()
        except Exception as e:
            logger.warning(f"Wiki 索引页维护失败: {e}")

    async def _get_kb_owner(self, db, kb_id: str) -> Optional[str]:
        from sqlalchemy import select

        from src.models.knowledge_base import KnowledgeBase

        try:
            kb_uuid = uuid.UUID(str(kb_id))  # KB 主键为 UUID 列，字符串主键场景直接跳过
        except ValueError:
            return None
        try:
            row = (
                await db.execute(
                    select(KnowledgeBase).filter(KnowledgeBase.id == kb_uuid)
                )
            ).scalars().first()
            return row.owner_id if row else None
        except Exception:
            return None


# ----------------------------------------------------------------------
# 模块级工具函数
# ----------------------------------------------------------------------


def normalize_doc_ids(doc_ids: Union[str, Sequence[str]]) -> List[str]:
    """doc_ids 归一化：单值 str / 单元素集合统一为去重后的字符串列表（保持顺序）。"""
    if isinstance(doc_ids, str):
        return [doc_ids]
    seen: List[str] = []
    for d in doc_ids:
        s = str(d)
        if s not in seen:
            seen.append(s)
    return seen


def build_material(chunks, max_chars: int) -> str:
    """拼接 chunks 正文为编译材料并按 max_chars 截断（compile_pages 与矛盾抽查共用）。"""
    return "\n\n".join(
        chunk.page_content for chunk in chunks if getattr(chunk, "page_content", "")
    )[:max_chars]


# Wiki 正文交叉链接语法：[[Title]]
_WIKI_LINK_RE = re.compile(r"\[\[([^\[\]]+)\]\]")


def extract_links(content: str, self_title: str, valid_titles) -> List[str]:
    """提取正文 [[Title]] 链接：去重、排除自引用与不存在页。

    Args:
        content: 页面正文
        self_title: 本页标题（自引用排除，归一化比较）
        valid_titles: 有效目标标题集合/映射（归一化 key → 原标题）；
                      存在性校验后的链接写回原标题

    Returns:
        去重后的目标标题列表（保留首次出现顺序）
    """
    valid = (
        valid_titles
        if isinstance(valid_titles, dict)
        else {normalize_title(t): t for t in (valid_titles or [])}
    )
    self_norm = normalize_title(self_title)
    seen = set()
    links = []
    for raw in _WIKI_LINK_RE.findall(content or ""):
        title = raw.strip()[:256]
        norm = normalize_title(title)
        if not norm or norm == self_norm or norm in seen:
            continue
        target = valid.get(norm)
        if target is None:
            continue
        seen.add(norm)
        links.append(target)
    return links


def normalize_title(title: str) -> str:
    """标题归一化：去空白转小写（精确匹配快路径）。"""
    return re.sub(r"\s+", "", (title or "")).lower()


def _split_sentences(text: str) -> List[str]:
    """按中英文句末标点与换行切句（诊断探针用），去空句。"""
    parts = re.split(r"(?<=[。！？!?；;])|\n", text or "")
    return [p.strip() for p in parts if p and p.strip()]


def _strip_think(text: str) -> str:
    """剥离混合思考模型可能输出的 <think> 块。"""
    return _THINK_RE.sub("", text or "")


def _parse_json_dict(text: str) -> dict:
    """从 LLM 输出中解析 JSON 对象；容忍代码围栏与包裹文字。"""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text, flags=re.DOTALL)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_BLOCK_RE.search(text)
        if not m:
            raise
        data = json.loads(m.group(0))
    return data if isinstance(data, dict) else {}


def _cosine(vec_a: List[float], vec_b: List[float]) -> float:
    """余弦相似度；非法输入返回 0（对齐 semantic_cache_service 语义）。"""
    try:
        if not vec_a or not vec_b or len(vec_a) != len(vec_b):
            return 0.0
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        na = sum(a * a for a in vec_a) ** 0.5
        nb = sum(b * b for b in vec_b) ** 0.5
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)
    except TypeError:
        return 0.0
