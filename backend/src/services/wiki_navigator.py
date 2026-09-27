"""Wiki 导航器（阶段一 D1，见 docs/design/wiki-navigable-workspace.md）。

为 wiki_lookup 工具提供两类能力，把 Wiki 编译层从"被动检索语料"升级为
"Agent 可导航的工作空间"（Karpathy Pattern 的 query 工作流）：

1. search_entries：入口检索——按 query 定位编译页切片（含 synthesis 页），
   结果附带各命中页的 links（Related），供 Agent 决定是否继续行走；
2. get_page_by_title：按 (kb_ids, 归一化标题) 精确取页——从向量库按
   document_id 取回整页分块拼接（与 wiki_link_expansion 同一取数口径），
   截断 WIKI_NAV_PAGE_MAX_CHARS，附带 Related 链接。

所有失败均 fail-soft：返回空结果/错误信息，不向 Agent 抛异常。
"""

import logging
from typing import Dict, List, Optional

from src.config import settings

logger = logging.getLogger("rag_system")

# 入口检索默认取回的编译页切片数
ENTRY_TOP_K = 4
# 入口结果末尾 Related 链接最多展示数
MAX_RELATED = 8
# synthesis 页在 Milvus 中的 source_kind（≤16 字符，与 schema 一致）
SYNTHESIS_SOURCE_KIND = "wiki_syn"


class WikiNavigator:
    """无状态导航服务（方法内懒加载依赖，便于单测注入）。"""

    def __init__(self, kb_retrieval_service=None, vector_store=None):
        self._kb_retrieval_service = kb_retrieval_service
        self._vector_store = vector_store

    # ------------------------------------------------------------------
    # 依赖懒加载
    # ------------------------------------------------------------------
    async def _ensure_retrieval_service(self):
        if self._kb_retrieval_service is None:
            from src.services.kb_retrieval_service import KBRetrievalService

            self._kb_retrieval_service = KBRetrievalService()
        return self._kb_retrieval_service

    async def _ensure_vector_store(self):
        if self._vector_store is None:
            from src.services.vector_store import VectorStoreManager

            self._vector_store = await VectorStoreManager.get_instance()
        return self._vector_store

    # ------------------------------------------------------------------
    # 入口检索
    # ------------------------------------------------------------------
    async def search_entries(self, query: str, kb_ids: List[str], top_k: int = ENTRY_TOP_K) -> List:
        """入口检索：合并 wiki 与 wiki_syn 两类编译页切片，返回 Document 列表。

        synthesis 页（source_kind="wiki_syn"）不参与主检索混入，仅经此入口
        与链接导航触达；两路并行召回后按原始顺序合并截断。
        命中切片的 metadata 会附带 page_links（该页的 Related 链接），供
        工具层聚合为 Related 行——任何失败不影响切片本身。
        """
        service = await self._ensure_retrieval_service()
        kinds = ("wiki", SYNTHESIS_SOURCE_KIND)
        merged: List = []
        for kind in kinds:
            try:
                docs = await service.retrieve(
                    query, kb_ids=kb_ids, source_kind=kind
                )
                merged.extend(docs or [])
                if len(merged) >= top_k * len(kinds):
                    break
            except Exception as e:
                logger.warning(f"Wiki 入口检索失败（kind={kind}，忽略）: {e}")
        merged = merged[: top_k * 2]
        await self._attach_page_links(merged)
        return merged

    async def _attach_page_links(self, docs: List) -> None:
        """按命中切片的 document_id 查 wiki_pages.links，写入 metadata.page_links。"""
        page_ids = []
        for doc in docs:
            meta = doc.metadata if hasattr(doc, "metadata") else {}
            pid = str(meta.get("document_id", ""))
            if meta.get("source_kind") in ("wiki", SYNTHESIS_SOURCE_KIND) and pid:
                page_ids.append(pid)
        if not page_ids:
            return
        try:
            from sqlalchemy import select

            from src.database import async_session_maker
            from src.models.wiki_page import WikiPage

            async with async_session_maker() as db:
                rows = (
                    await db.execute(
                        select(WikiPage).filter(
                            WikiPage.id.in_(page_ids),
                            WikiPage.status == "active",
                        )
                    )
                ).scalars().all()
            links_by_id = {str(r.id): list(r.links or []) for r in rows}
            for doc in docs:
                meta = doc.metadata if hasattr(doc, "metadata") else {}
                links = links_by_id.get(str(meta.get("document_id", "")))
                if links:
                    try:
                        meta["page_links"] = links
                    except Exception:
                        pass  # metadata 只读等异常场景直接跳过
        except Exception as e:
            logger.warning(f"Wiki Related 链接附加失败（不影响入口结果）: {e}")

    # ------------------------------------------------------------------
    # 按页取读
    # ------------------------------------------------------------------
    async def get_page_by_title(self, kb_ids: List[str], title: str) -> Optional[Dict]:
        """按 (kb_ids, 归一化标题) 精确取页：元数据 + 全文（截断）+ Related。

        Returns:
            {"page_id", "kb_id", "title", "page_type", "content", "links"} 或 None。
        """
        if not kb_ids or not title:
            return None
        try:
            from sqlalchemy import select

            from src.database import async_session_maker
            from src.models.wiki_page import WikiPage
            from src.services.wiki_compiler import normalize_title

            want = normalize_title(title)
            async with async_session_maker() as db:
                rows = (
                    await db.execute(
                        select(WikiPage).filter(
                            WikiPage.kb_id.in_([str(k) for k in kb_ids]),
                            WikiPage.status == "active",
                        )
                    )
                ).scalars().all()
            row = next(
                (r for r in rows if normalize_title(r.title) == want), None
            )
            if row is None:
                return None

            content = await self._read_page_content(str(row.id))
            max_chars = settings.wiki_compile.WIKI_NAV_PAGE_MAX_CHARS
            if len(content) > max_chars:
                content = content[:max_chars] + "\n\n（内容过长，已截断；可用更具体的 Related 页面继续导航）"
            return {
                "page_id": str(row.id),
                "kb_id": str(row.kb_id),
                "title": row.title,
                "page_type": row.page_type,
                "content": content,
                "links": list(row.links or []),
            }
        except Exception as e:
            logger.warning(f"Wiki 按页取读失败（title={title}）: {e}")
            return None

    async def _read_page_content(self, page_id: str) -> str:
        """从向量库按 document_id 取回整页分块，按 chunk_index 拼接。"""
        vector_store = await self._ensure_vector_store()
        chunks = await vector_store.get_chunks_by_document_id(page_id)
        if not chunks:
            return ""
        chunks = sorted(chunks, key=lambda d: d.metadata.get("chunk_index", 0))
        return "\n".join(
            d.page_content for d in chunks if getattr(d, "page_content", "")
        )


def format_related(links: List[str], exclude_titles: Optional[List[str]] = None) -> str:
    """把页面的 links 列表格式化为 Related 行（排除命中页自身，去重截断）。

    供 Agent 决定是否沿链接继续取读；无链接时返回空串。
    """
    if not links:
        return ""
    exclude = {str(t) for t in (exclude_titles or [])}
    seen, shown = set(), []
    for t in links:
        if t in exclude or t in seen:
            continue
        seen.add(t)
        shown.append(f"[[{t}]]")
        if len(shown) >= MAX_RELATED:
            break
    if not shown:
        return ""
    return "Related: " + " ".join(shown)
