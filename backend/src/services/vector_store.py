"""向量存储管理器。

对 MilvusService 进行二次封装，将原始检索结果转换为 LangChain Document 对象，
供 RAG 链统一使用。
"""

import logging

from langchain_core.documents import Document
from pymilvus.exceptions import MilvusException

from src.services.chunk_expand import (
    SECTION_SEP,
    bare_heading,
    merge_appended,
    merge_with_neighbors,
    neighbor_positions,
    same_heading_as_parent,
    section_prefix,
    section_prefixes,
    similarity_gap,
    split_clauses,
)
from src.services.milvus_service import MilvusService
from src.utils.async_singleton import AsyncSingleton
from src.utils.error_handling import ErrorSeverity, log_exception

logger = logging.getLogger(__name__)


class VectorStoreManager(AsyncSingleton["VectorStoreManager"]):
    """向量存储管理器，负责 Document 与 Milvus 之间的转换。"""

    def __init__(self):
        self.milvus_service = None

    async def _async_init(self):
        """异步初始化 MilvusService 连接。"""
        self.milvus_service = await MilvusService.get_instance()

    async def create_vector_store(self, documents, kb_id=""):
        """首次创建向量存储并插入文档（语义同 add_documents）。"""
        await self.milvus_service.insert_embeddings(documents, kb_id)
        return self.milvus_service

    async def add_documents(self, documents, kb_id=""):
        """向指定知识库追加文档切片。"""
        await self.milvus_service.insert_embeddings(documents, kb_id)

    async def search(self, query, k=3, document_ids=None, kb_ids=None):
        """检索与查询最相关的文档切片，并包装为 LangChain Document。

        阶段一升级：默认走混合检索（dense + BM25 + RRF + rerank），
        当混合检索未启用或失败时自动回退到纯 dense 检索。

        Args:
            query: 用户查询文本。
            k: 返回结果数量上限。
            document_ids: 可选，限定文档范围。
            kb_ids: 可选，限定知识库范围。

        Returns:
            list[Document]: 包含元数据的 LangChain Document 列表。
        """
        try:
            results = await self.milvus_service.search_hybrid(
                query, k=k, document_ids=document_ids, kb_ids=kb_ids
            )
        except MilvusException as e:
            # 仅捕获 Milvus 已知可恢复异常降级纯向量检索；其它异常（配置错误等）上抛。
            # 降级后少了两路召回，结果排序与命中率都会变——用户只会觉得「检索变差」。
            log_exception(
                logger, e,
                action="vector_store.search_hybrid",
                severity=ErrorSeverity.RECOVERABLE,
                message="混合检索失败，降级为纯向量检索",
                k=k,
                kb_scope=len(kb_ids) if kb_ids else 0,
            )
            results = await self.milvus_service.search(
                query, k=k, document_ids=document_ids, kb_ids=kb_ids
            )
        return self._results_to_documents(results)

    async def search_dense(self, query, k=3, document_ids=None, kb_ids=None, query_embedding=None, source_kind=None, exclude_source_kinds=None):
        """纯 dense 向量检索入口（兼容旧逻辑）。"""
        results = await self.milvus_service.search_dense(
            query, k=k, document_ids=document_ids, kb_ids=kb_ids, query_embedding=query_embedding, source_kind=source_kind, exclude_source_kinds=exclude_source_kinds
        )
        return self._results_to_documents(results)

    async def search_hybrid(self, query, k=3, document_ids=None, kb_ids=None, query_embedding=None, source_kind=None, exclude_source_kinds=None):
        """混合检索入口，返回经 RRF 融合与 Cross-Encoder 重排序后的 Document。"""
        results = await self.milvus_service.search_hybrid(
            query, k=k, document_ids=document_ids, kb_ids=kb_ids, query_embedding=query_embedding, source_kind=source_kind, exclude_source_kinds=exclude_source_kinds
        )
        return self._results_to_documents(results)

    async def search_hybrid_multi(self, queries, k=3, document_ids=None, kb_ids=None, query_embedding=None, source_kind=None, exclude_source_kinds=None):
        """多查询并行混合检索入口（P2-2），返回经跨查询 RRF 融合与重排序后的 Document。

        queries[0] 必须为原始（或消解后）问题，作为 rerank 语义基准。
        """
        results = await self.milvus_service.search_hybrid_multi(
            queries, k=k, document_ids=document_ids, kb_ids=kb_ids, query_embedding=query_embedding, source_kind=source_kind, exclude_source_kinds=exclude_source_kinds
        )
        return self._results_to_documents(results)

    async def expand_neighbors(self, docs, budget_chars=None):
        """给选中的块补上同文档的 `chunk_index ± 1`（邻块回填）。

        调用点必须在**截断到 TOP_K 之后**：补进来的邻块排在原地块后面，
        先补后截断等于把它们全部截掉（离线脚本是在探针结果上直接算的，
        接进生产时这是最容易出错的一处）。

        Args:
            docs: 已按相关性排序、已截断的 Document 列表。
            budget_chars: 总字符上限（含原地块），None 表示不限。见 chunk_expand 的实测数据。

        Returns:
            原地块 + 邻块。补查失败时原样返回原地块（回填是增益，不是依赖）。
        """
        if not docs or budget_chars == 0:
            return docs
        # 用 (document_id, chunk_index) 当 id：切片主键是 uuid、每次重灌都会变，
        # 位置才是稳定身份；而且相邻的两个原地块互为邻块时，靠它去重才不会补出重复块。
        base = []
        for d in docs:
            doc_id = str(d.metadata.get("document_id", ""))
            idx = d.metadata.get("chunk_index")
            if not doc_id or idx is None:
                continue
            base.append({
                "id": f"{doc_id}#{int(idx)}",
                "document_id": doc_id,
                "chunk_index": int(idx),
                "content": d.page_content,
                "heading_path": str(d.metadata.get("heading_path") or ""),
                "_doc": d,
            })
        if not base:
            return docs
        positions = neighbor_positions(base)
        if not positions:
            return docs
        try:
            rows = await self.milvus_service.fetch_chunks_by_positions(positions)
        except Exception as e:  # noqa: BLE001 —— 回填失败不能影响检索主链路
            log_exception(
                logger, e, action="vector_store.expand_neighbors",
                severity=ErrorSeverity.RECOVERABLE,
                message="邻块回填失败，返回原检索结果",
                positions=len(positions),
            )
            return docs
        if not rows:
            return docs

        for row in rows:
            row["id"] = f'{row.get("document_id")}#{int(row.get("chunk_index", -1))}'
        # 物理相邻 ≠ 语义连续：只补同章节的邻块，跨章节的相邻块会把注意力带偏
        # （实测代价见 chunk_expand.same_heading_as_parent 的说明）
        rows = same_heading_as_parent(rows, base)
        if not rows:
            return docs
        merged = merge_with_neighbors(base, rows, budget_chars=budget_chars)

        # 邻块没有自己的重排分：沿用「带它进来的那个原地块」的分数。
        # 不编一个 0 分，否则下游按分数排序或做相关性门禁时，邻块会莫名其妙沉底/被清掉。
        parent_score = {
            (str(d.metadata.get("document_id", "")), int(d.metadata.get("chunk_index") or -1)): float(
                d.metadata.get("rerank_score") or 0.0
            )
            for d in docs
        }
        out = []
        n_added = 0
        for item in merged:
            if item.get("_doc") is not None:
                out.append(item["_doc"])
                continue
            doc = self._results_to_documents([item])[0]
            key = (item.get("document_id", ""), int(item.get("chunk_index") or -1))
            score = parent_score.get((key[0], key[1] - 1), parent_score.get((key[0], key[1] + 1), 0.0))
            doc.metadata["rerank_score"] = score
            doc.metadata["score"] = score
            doc.metadata["neighbor_expand"] = True
            out.append(doc)
            n_added += 1
        logger.debug(
            "邻块回填：%d 块 → %d 块（+%d，预算 %s 字符）",
            len(docs), len(out), n_added, budget_chars,
        )
        return out

    async def expand_sections(
        self,
        docs,
        budget_chars=None,
        max_sections=5,
        max_distance=12,
        skip_score=None,
        min_section_score=None,
        query=None,
        title_gap=None,
    ):
        """把入选块所属**二级小节**的其余块补齐（整节回填）。

        与 `expand_neighbors` 的分工：那个补「物理相邻的一块」，这个补「同一小节的
        全部块」。前者解决「步骤被切成两半」，后者解决「最相关的那块根本没排进
        top_k」——后者是 ±1 够不到的：实测 §4.2「恢复后必做」在候选池第 12 名
        （重排分 0.064），要补它就必须跳过重排、按文档结构取数。

        调用点同样必须在**截断到 TOP_K 之后**，否则补进来的块会被一并截掉。

        Args:
            docs: 已排序、已截断的 Document 列表（通常是邻块回填后的结果）。
            budget_chars: 总字符上限（含现有全部块）。None 表示不限。
            max_sections: 最多回填几个小节，避免一次请求铺开十几节。
            max_distance: 只补离原地块 `chunk_index` 差不超过该值的块。太远的块
                多半是同一 H2 下的另一张表/另一个子主题（实测 7.3 演练记录里
                相距 40 块的两段表格），补进来只有噪声。None 表示不限。
            skip_score: 高分跳过阈值。**原始入选块**（不含邻块/整节块）的最高重排分
                达到该值时直接不补——重排已经把答案放在手里，再按结构铺开只会稀释
                注意力（实测「前后端监听哪个端口」5 块 → 24 块，答案并未变好）。
                None 表示不启用。
            min_section_score: 小节分数下限。只补父块分数 ≥ 该值的小节；None 表示不筛。
                为什么要筛：一场问答常同时命中「正文小节」和「沾边小节」，后者（如
                5.1 决策矩阵那种一行一块的表格）块数多、分数低，铺开后会把正文挤出
                注意力的中心——实测 16 块里 9 块来自 §5，答案从 7 步缩成 3 行。
            query: 原始问题。只有传了它才能启用 `title_gap` 闸门。
            title_gap: 子节标题闸门（与 `min_section_score` 是两道独立闸门）。
                逐节算「未命中子节标题」与问题子句的最高相似度，减去「已命中子节标题」
                的最高相似度；差值 < 该值的节不铺。None 表示不启用。
                为什么需要：小节几何完全相同的两题可能一个该铺一个不该铺——差别在
                「问题是否指向了没命中的那个子节」，而分数、块数、节内覆盖率都看不见
                这件事（详见 `chunk_expand` 里的实测记录）。

        Returns:
            原地块在前，整节其余块按「小节轮转」追加：小节之间按父块分数降序，
            小节内部按结构距离升序。补查失败或层级不足（只有两级标题）时原样返回。
        """
        if not docs or budget_chars == 0:
            return docs
        base = []
        for d in docs:
            doc_id = str(d.metadata.get("document_id", ""))
            idx = d.metadata.get("chunk_index")
            if not doc_id or idx is None:
                continue
            base.append({
                "id": f"{doc_id}#{int(idx)}",
                "document_id": doc_id,
                "chunk_index": int(idx),
                "content": d.page_content,
                "heading_path": str(d.metadata.get("heading_path") or ""),
                "_doc": d,
            })
        if not base:
            return docs
        # 高分跳过：只看**原始入选块**。邻块继承父块分数，拿它们参与判定等于自己给
        # 自己投票；而首块分数特别高时（如 0.97）说明重排已定位到答案，补整节是纯噪声。
        if skip_score is not None:
            original = [
                float(d.metadata.get("rerank_score") or 0.0)
                for d in docs
                if not d.metadata.get("neighbor_expand")
                and not d.metadata.get("section_expand")
            ]
            top = max(original) if original else 0.0
            if top >= skip_score:
                logger.debug(
                    "整节回填跳过：首块重排分 %.3f ≥ 阈值 %.3f（%d 块保持原样）",
                    top, skip_score, len(docs),
                )
                return docs
        pairs = section_prefixes(base)[:max_sections]
        if not pairs:
            return docs

        try:
            rows = await self.milvus_service.fetch_chunks_by_heading_prefix(pairs)
        except Exception as e:  # noqa: BLE001 —— 回填失败不能影响检索主链路
            log_exception(
                logger, e, action="vector_store.expand_sections",
                severity=ErrorSeverity.RECOVERABLE,
                message="整节回填失败，返回原检索结果",
                sections=len(pairs),
            )
            return docs
        if not rows:
            return docs

        for row in rows:
            row["id"] = f'{row.get("document_id")}#{int(row.get("chunk_index", -1))}'

        # 追加顺序：按小节「轮转」，小节之间按父块分数降序，小节内部按结构距离升序。
        #
        # 为什么必须轮转，而不是「先排完一个再排下一个」「按距离全局排」：
        # 排名第一的块常常属于一个占满位置的表格小节（本例 7.3 演练记录 5 块表格行）。
        # 两种朴素排法都会让它先吃满预算，真正需要的小节被挤出上下文——
        # 2026-11-24 实测：按小节顺序排时 §7.3 先吃掉 2171 字符，§4.2「恢复后必做」
        # 照旧没进来；按距离全局排时 §7.3 的相邻表格行仍然排在 §4.2 前面。
        # 轮转保证「每个小节都能轮到一次」，预算在它们之间摊开。
        section_parents: dict = {}
        section_score: dict = {}
        for d in docs:
            doc_id = str(d.metadata.get("document_id", ""))
            prefix = section_prefix(str(d.metadata.get("heading_path") or ""))
            idx = d.metadata.get("chunk_index")
            if idx is None:
                continue
            section_parents.setdefault((doc_id, prefix), []).append(int(idx))
            score = float(d.metadata.get("rerank_score") or 0.0)
            key = (doc_id, prefix)
            section_score[key] = max(section_score.get(key, 0.0), score)

        def _section_key(row):
            return (
                str(row.get("document_id", "")),
                section_prefix(str(row.get("heading_path") or "")),
            )

        def _distance(row):
            parents = section_parents.get(_section_key(row)) or []
            idx = int(row.get("chunk_index", -1))
            if not parents:
                return 0
            return min(abs(idx - p) for p in parents)

        groups: dict = {}
        for row in rows:
            dist = _distance(row)
            # 太远的块多半是同一 H2 下的另一张表/另一个子主题，补进来只有噪声
            if max_distance is not None and dist > max_distance:
                continue
            groups.setdefault(_section_key(row), []).append((dist, row))
        for items in groups.values():
            items.sort(key=lambda pair: (pair[0], int(pair[1].get("chunk_index", -1))))

        ordered_sections = sorted(
            groups, key=lambda k: (-section_score.get(k, 0.0), str(k))
        )
        if min_section_score is not None:
            eligible = [
                k for k in ordered_sections if section_score.get(k, 0.0) >= min_section_score
            ]
            if not eligible:
                logger.debug(
                    "整节回填跳过：%d 个小节分数均低于下限 %.3f",
                    len(ordered_sections), min_section_score,
                )
                return docs
            ordered_sections = eligible
        if title_gap is not None and query:
            gaps = await self._section_title_gaps(query, ordered_sections, groups, docs)
            if gaps is not None:
                kept = [k for k in ordered_sections if (gaps.get(k) or -1.0) >= title_gap]
                if not kept:
                    logger.debug(
                        "整节回填跳过：%d 个小节的子节标题缺口均低于 %.2f",
                        len(ordered_sections), title_gap,
                    )
                    return docs
                ordered_sections = kept
        interleaved: list = []
        if ordered_sections:
            depth = max(len(groups[k]) for k in ordered_sections)
            for i in range(depth):
                for key in ordered_sections:
                    bucket = groups[key]
                    if i < len(bucket):
                        interleaved.append(bucket[i][1])

        merged = merge_appended(base, interleaved, budget_chars=budget_chars)

        # 整节补进来的块沿用「把它带进来的那个原地块」的分数：
        # 不编 0 分，否则下游按分数排序或做相关性门禁时它会莫名沉底/被清掉。
        parent_score = section_score

        out = []
        n_added = 0
        for item in merged:
            if item.get("_doc") is not None:
                out.append(item["_doc"])
                continue
            doc = self._results_to_documents([item])[0]
            score = parent_score.get(_section_key(item), 0.0)
            doc.metadata["rerank_score"] = score
            doc.metadata["score"] = score
            doc.metadata["section_expand"] = True
            out.append(doc)
            n_added += 1
        logger.debug(
            "整节回填：%d 块 → %d 块（+%d，小节 %d 个，预算 %s 字符）",
            len(docs), len(out), n_added, len(pairs), budget_chars,
        )
        return out

    async def _section_title_gaps(self, query, sections, groups, docs):
        """逐节算「未命中子节标题 − 已命中子节标题」与问题子句的相似度差。

        Returns:
            dict[key, float]，或 None 表示算不出来（无子句 / 无子节标题 / embedding
            不可用）。**失败必须降级成「照常铺」，不能降级成「都不铺」**——后者会让
            一次 embedding 抖动把整节回填静默关掉，且只在日志里留一行 debug。
        """
        clauses = split_clauses(query)
        if not clauses or not sections:
            return None

        def h3_of(path) -> str:
            seg = [s.strip() for s in str(path or "").split(SECTION_SEP)]
            return bare_heading(seg[2]) if len(seg) >= 3 else ""

        all_titles: dict = {}
        for key, items in groups.items():
            for _dist, row in items:
                title = h3_of(row.get("heading_path"))
                if title:
                    all_titles.setdefault(key, set()).add(title)

        hit_titles: dict = {}
        for d in docs:
            key = (
                str(d.metadata.get("document_id", "")),
                section_prefix(str(d.metadata.get("heading_path") or "")),
            )
            title = h3_of(d.metadata.get("heading_path"))
            if title:
                hit_titles.setdefault(key, set()).add(title)

        pool = sorted({t for k in sections for t in all_titles.get(k, ())})
        if not pool:
            return None
        try:
            embed = self.milvus_service.embeddings
            clause_vecs = await embed.aembed_documents(clauses)
            title_vecs = await embed.aembed_documents(pool)
        except Exception as e:  # noqa: BLE001 —— 判据算不出来就退化成「照常铺」
            log_exception(
                logger, e, action="vector_store.section_title_gaps",
                severity=ErrorSeverity.RECOVERABLE,
                message="子节标题闸门计算失败，本次不启用该闸门",
                sections=len(sections),
            )
            return None

        vec_of = dict(zip(pool, title_vecs))
        out = {}
        for key in sections:
            hit_set = hit_titles.get(key, set())
            hit_vecs = [vec_of[t] for t in hit_set if t in vec_of]
            unhit_vecs = [
                vec_of[t] for t in all_titles.get(key, ())
                if t not in hit_set and t in vec_of
            ]
            gap = similarity_gap(clause_vecs, hit_vecs, unhit_vecs)
            if gap is not None:
                out[key] = gap
        logger.debug(
            "子节标题缺口：%s",
            {k[1].split(SECTION_SEP)[-1]: round(v, 3) for k, v in out.items()},
        )
        return out

    def _results_to_documents(self, results):
        """将 Milvus 检索结果统一转换为 LangChain Document。"""
        docs = []
        for result in results:
            doc = Document(
                page_content=result["content"],
                metadata={
                    "kb_id": result.get("kb_id", ""),
                    "document_id": result.get("document_id", ""),
                    "source": result.get("source", ""),
                    "heading_path": result.get("heading_path", ""),
                    "chunk_index": result.get("chunk_index", 0),
                    "source_kind": result.get("source_kind", "raw"),
                    "score": result.get("rerank_score", result.get("rrf_score", result.get("score", 0.0))),
                    "dense_score": result.get("dense_score", 0.0),
                    "sparse_score": result.get("sparse_score", 0.0),
                    "rrf_score": result.get("rrf_score", 0.0),
                    "rerank_score": result.get("rerank_score", 0.0),
                    # 重排整批失败时 hybrid_search 回退 RRF，并把 flag 透传到这里：
                    # 下游据此知道「rerank_score 不是相关性分」，不能拿它做相关性门禁
                    "rerank_fallback": bool(result.get("rerank_fallback", False)),
                }
            )
            docs.append(doc)
        return docs

    async def get_chunks_by_document_id(self, document_id):
        """获取指定文档（含 Wiki 页）的全部分块，包装为 Document（Wiki 链接扩展用）。"""
        results = await self.milvus_service.get_document_chunks(document_id)
        return self._results_to_documents(results)

    async def delete_by_document_id(self, document_id):
        """按文档 ID 删除向量数据。"""
        await self.milvus_service.delete_by_document_id(document_id)

    async def delete_by_document_ids(self, document_ids):
        """按多个文档 ID 批量删除向量数据，并统一 flush 一次。"""
        await self.milvus_service.delete_by_document_ids(document_ids)

    async def delete_by_kb_id(self, kb_id):
        """按知识库 ID 删除向量数据。"""
        await self.milvus_service.delete_by_kb_id(kb_id)

    async def delete_by_kb_ids(self, kb_ids):
        """按多个知识库 ID 批量删除向量数据，并统一 flush 一次。"""
        await self.milvus_service.delete_by_kb_ids(kb_ids)

    async def flush(self):
        """显式触发 Milvus flush。"""
        await self.milvus_service.flush()

    async def count(self):
        """返回全部切片数量。"""
        return await self.milvus_service.count()

    async def count_by_kb(self, kb_id):
        """返回指定知识库的切片数量。"""
        return await self.milvus_service.count_by_kb(kb_id)
