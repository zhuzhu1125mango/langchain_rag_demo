"""交叉链接检索扩展（P3 LLM-Wiki 编译层 Phase 3，§11.2）。

rerank 之后、context 组装之前追加：扫描检索结果中 source_kind="wiki" 的页 →
读 wiki_pages.links → 按 (kb_id, title) 查目标页 → 取目标页分块直接追加进
context。每页补 2 块、总上限 6 块；扩展块 metadata 标 link_expanded=true 可追溯，
不挤占原命中。开关 WIKI_LINK_EXPANSION（默认关）；无链接/查询失败 → 原结果返回。
"""

import logging
from typing import List

from src.config import settings
from src.services.wiki_compiler import normalize_title

logger = logging.getLogger("wiki_link_expansion")

# 每个目标页补入的分块数
CHUNKS_PER_PAGE = 2
# 单次扩展总块数上限
MAX_EXPANDED_CHUNKS = 6


async def expand_wiki_links(docs: List) -> List:
    """对 rerank 后的检索结果做交叉链接扩展；任何失败均返回原结果。

    Args:
        docs: rerank 后的 Document 列表（原命中保持在前，不被挤占）

    Returns:
        原结果 + 追加的扩展块（metadata.link_expanded=True）
    """
    if not docs or not settings.wiki_compile.WIKI_LINK_EXPANSION:
        return docs
    try:
        from sqlalchemy import select

        from src.database import async_session_maker
        from src.models.wiki_page import WikiPage
        from src.services.vector_store import VectorStoreManager

        # 1. 收集检索结果中的 wiki 页（按命中顺序）
        wiki_doc_ids: List[str] = []
        for doc in docs:
            page_id = str(doc.metadata.get("document_id", ""))
            if doc.metadata.get("source_kind") == "wiki" and page_id:
                if page_id not in wiki_doc_ids:
                    wiki_doc_ids.append(page_id)
        if not wiki_doc_ids:
            return docs

        async with async_session_maker() as db:
            source_rows = (
                await db.execute(
                    select(WikiPage).filter(
                        WikiPage.id.in_(wiki_doc_ids),
                        WikiPage.status == "active",
                    )
                )
            ).scalars().all()
            rows_by_id = {str(r.id): r for r in source_rows}
            link_titles = {
                t for r in source_rows for t in (r.links or [])
            }
            if not link_titles:
                return docs

            # 2. 按 (kb_id, 归一化标题) 定位目标页（仅同一 KB 内）
            kb_ids = {str(r.kb_id) for r in source_rows}
            target_rows = (
                await db.execute(
                    select(WikiPage).filter(
                        WikiPage.kb_id.in_(kb_ids),
                        WikiPage.status == "active",
                    )
                )
            ).scalars().all()
            targets = {
                (str(r.kb_id), normalize_title(r.title)): r
                for r in target_rows
                if str(r.id) not in rows_by_id  # 目标页排除命中页自身
            }

            # 3. 按命中顺序遍历链接，补目标页分块（每页 2 块、总上限 6 块）
            vector_store = await VectorStoreManager.get_instance()
            expanded = []
            visited_targets = set()
            for page_id in wiki_doc_ids:
                row = rows_by_id.get(page_id)
                if row is None:
                    continue
                for title in row.links or []:
                    if len(expanded) >= MAX_EXPANDED_CHUNKS:
                        break
                    target = targets.get((str(row.kb_id), normalize_title(title)))
                    if target is None or str(target.id) in visited_targets:
                        continue
                    visited_targets.add(str(target.id))
                    chunks = await vector_store.get_chunks_by_document_id(str(target.id))
                    chunks.sort(key=lambda d: d.metadata.get("chunk_index", 0))
                    for chunk in chunks[:CHUNKS_PER_PAGE]:
                        chunk.metadata["link_expanded"] = True
                        chunk.metadata["score"] = 0.0  # 不挤占原命中的排序
                        expanded.append(chunk)

            if not expanded:
                return docs
            logger.info(f"Wiki 链接扩展: 追加 {len(expanded)} 块（来源页 {len(wiki_doc_ids)} 个）")
            return list(docs) + expanded[:MAX_EXPANDED_CHUNKS]
    except Exception as e:
        logger.warning(f"Wiki 链接扩展失败，返回原结果: {e}")
        return docs
