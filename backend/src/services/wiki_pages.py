"""LLM-Wiki 页面编译：候选抽取、页面匹配、生成、精炼与事实校验（CODE-1 拆分）。"""

import logging
from typing import Dict, List, Optional

from src.config import settings

from src.services.wiki_types import (
    FACT_RETENTION_TARGET,
    FACT_RETENTION_WARN,
    FACT_RETENTION_THRESHOLD,
    TITLE_MATCH_THRESHOLD,
    CompiledPage,
    ExistingPage,
)
from src.services.wiki_text import (
    _cosine,
    _parse_json_dict,
    _split_sentences,
    _strip_think,
    normalize_title,
)
from src.utils.error_handling import ErrorSeverity, log_exception

logger = logging.getLogger("wiki_compiler")

class PageCompileMixin:
    """页面生成/匹配/精炼，由 WikiCompiler 组合。"""

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
                resp = await self._ainvoke(prompt)
                pages = _parse_json_dict(_strip_think(resp.content)).get("pages", [])
                break
            except Exception as e:
                if attempt == 1:
                    log_exception(
                        logger, e,
                        action="wiki_compile.extract_candidates",
                        severity=ErrorSeverity.RECOVERABLE,
                        message="Wiki 候选抽取失败，重试一次",
                    )

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
                log_exception(
                    logger, e,
                    action="wiki_compile.match_by_embedding",
                    severity=ErrorSeverity.RECOVERABLE,
                    message="Wiki 页标题 embedding 匹配失败，剩余候选按新页处理",
                )
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
        # 页面与向量配对成元组列表，消费时同步删除，保证下标不错位
        pairs = list(zip(remaining, page_vecs))

        for pos, i in enumerate(pending_idx):
            best_score, best = 0.0, None
            best_idx = -1
            # 关键：page_vecs 在循环外按 remaining 的初始顺序一次性算出。
            # 必须把「页面」与「向量」配对成元组列表一起消费、一起删除，
            # 否则循环内 remaining.remove(best) 改变列表下标后，后续候选的
            # page_vecs[j] 会与错位的向量比较——命中一次后余下候选都在跟
            # "别人的向量"比相似度，匹配到错误既有页并静默合并。
            for idx, (page, vec) in enumerate(pairs):
                score = _cosine(cand_vecs[pos], vec)
                if score > best_score:
                    best_score = score
                    best = page
                    best_idx = idx
            if best is not None and best_score >= TITLE_MATCH_THRESHOLD:
                matched[i] = best
                remaining.remove(best)
                pairs.pop(best_idx)  # 同步移除向量，保持配对不错位
        return matched

    async def _generate_page(
        self, cand: dict, existing: Optional[ExistingPage], material: str
    ) -> str:
        """生成新页或增量合并既有页（约束：保留既有事实，矛盾显式标注）。

        注（2026-09-23）：曾试验在 prompt 中注入既有页标题要求 [[互链]]，
        多跳 A/B 证伪（链接文本混入正文使 B/C 臂与 D 臂同步退化），已回退；
        跨文档桥接的正规建法（图结构/独立链接元数据）待重新设计。
        """
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
        resp = await self._ainvoke(prompt)
        content = _strip_think(resp.content).strip()
        return content or f"# {title}\n\n（内容生成失败）"

    async def compile_synthesis_page(
        self,
        question: str,
        answer: str,
        cited_texts: List[str],
        existing_titles: List[str],
    ) -> Optional[CompiledPage]:
        """D3 答案回流：把「问题 + 已验证答案 + 引用片段」合成为 synthesis 页。

        与 compile_pages 的实体/主题抽取不同，synthesis 页直接以本次问答为
        材料（答案已经过引用补全与事实校验），LLM 负责蒸馏成可复用的知识页
        并用 [[既有页标题]] 与既有页面互链。失败返回 None（best-effort）。
        """
        try:
            material_parts = [f"用户问题：{question}", f"已验证答案：{answer}"]
            if cited_texts:
                joined = "\n---\n".join(t[:2000] for t in cited_texts[:6])
                material_parts.append(f"引用片段原文：\n{joined}")
            existing = "、".join(existing_titles[:30]) if existing_titles else "（暂无）"
            prompt = (
                "你是 Wiki 编辑。请把下面这次问答沉淀为一篇 Markdown 综合知识页，"
                "供后续直接回答同类问题使用。\n"
                "规则：\n"
                "- 第一行以「# 标题」开头，标题为该知识的主题名（不超过 20 字，不加书名号）\n"
                "- 只使用材料中的信息，不要编造；分小节陈述，保留关键数值与结论\n"
                "- 提到既有页面覆盖的概念时，用 [[既有页标题]] 引用\n"
                "- 末尾加「## 来源」小节，注明来自用户问答沉淀\n\n"
                f"既有页面标题（可用于 [[链接]]）：{existing}\n\n"
                + "\n\n".join(material_parts)
            )
            resp = await self._ainvoke(prompt)
            content = _strip_think(resp.content).strip()
            if not content:
                return None
            title = None
            for line in content.splitlines():
                if line.startswith("#"):
                    title = line.lstrip("#").strip()
                    break
            if not title:
                title = f"综合：{question[:24]}"
                content = f"# {title}\n\n{content}"
            title = title[:80]
            return CompiledPage(title=title, page_type="synthesis", content=content)
        except Exception as e:
            log_exception(
                logger, e,
                action="wiki_compile.synthesis_page",
                severity=ErrorSeverity.RECOVERABLE,
                message="synthesis 页合成失败，本次跳过该页",
            )
            return None

    # ------------------------------------------------------------------
    # 持久化层
    # ------------------------------------------------------------------

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
            log_exception(
                logger, e,
                action="wiki_compile.fact_retention_probe",
                severity=ErrorSeverity.RECOVERABLE,
                message="Wiki 诊断探针执行失败，不影响编译产物",
            )
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
                    log_exception(
                        logger, e,
                        action="wiki_compile.refine_page",
                        severity=ErrorSeverity.RECOVERABLE,
                        message="Wiki 页精炼失败，保留当前内容",
                        page_title=page.title,
                    )
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
        resp = await self._ainvoke(prompt)
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
        resp = await self._ainvoke(prompt)
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
                resp = await self._ainvoke(prompt)
                data = _parse_json_dict(_strip_think(resp.content))
                contradictions = [
                    str(c).strip()
                    for c in (data.get("contradictions") or [])
                    if str(c).strip()
                ]
            except Exception as e:
                log_exception(
                    logger, e,
                    action="wiki_compile.contradiction_probe",
                    severity=ErrorSeverity.RECOVERABLE,
                    message="Wiki 矛盾抽查失败，该页按无矛盾处理",
                    page_title=page.title,
                )
                continue
            if contradictions:
                record_wiki_contradictions(len(contradictions))
                for item in contradictions:
                    logger.warning(f"Wiki 矛盾抽查发现矛盾: 页「{page.title}」: {item}")

    @staticmethod

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
