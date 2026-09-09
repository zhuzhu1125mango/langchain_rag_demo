"""Wiki 级联清理（Phase 2）：源文档删除 / 知识库删除联动。

设计约定（llm-wiki-compile.md §9.2）：
- 失败不阻断删除主流程（对齐语义缓存失效钩子风格），内部捕获并记日志
- 孤儿页（失去全部来源支撑）直接删除：向量库 + MinIO + DB 三处清理
- 与编译共用按 KB 进程内锁，避免与进行中的编译互相踩踏
"""

import logging
from typing import List

from sqlalchemy import delete as sa_delete
from sqlalchemy import select

from src.services.wiki_compiler import get_kb_lock

logger = logging.getLogger("wiki_cascade")


async def on_document_deleted(db, kb_id: str, doc_id: str) -> None:
    """源文档删除后剪除 wiki_pages 来源；孤儿页整体删除并重建索引页。"""
    from src.models.wiki_page import WikiPage
    from src.services.wiki_compiler import WikiCompiler

    try:
        async with get_kb_lock(kb_id):
            rows = (
                await db.execute(
                    select(WikiPage).filter(
                        WikiPage.kb_id == str(kb_id),
                        WikiPage.status == "active",
                        # JSONB containment：source_doc_ids @> ["<doc_id>"]
                        WikiPage.source_doc_ids.contains([str(doc_id)]),
                    )
                )
            ).scalars().all()
            if not rows:
                return

            removed_pages = 0
            for row in rows:
                remaining = [d for d in (row.source_doc_ids or []) if d != str(doc_id)]
                if remaining:
                    # 仅剪源（页面内容未变，revision 不动）
                    row.source_doc_ids = remaining
                    continue
                # 孤儿页：无任何来源支撑，直接删除
                await _delete_page_artifacts(row)
                await db.delete(row)
                removed_pages += 1

            await db.commit()
            logger.info(
                f"Wiki 级联（源文档删除）: kb={kb_id}, doc={doc_id}, "
                f"受影响 {len(rows)} 页，删孤儿页 {removed_pages}"
            )
            if removed_pages:
                await WikiCompiler().rebuild_index_page(db, kb_id)
    except Exception as e:
        logger.warning(f"Wiki 级联（源文档删除）失败（不影响删除主流程）: {e}")


async def on_kb_deleted(db, kb_id: str) -> None:
    """知识库删除后清理 wiki_pages 行与 MinIO 正文（向量已由 delete_by_kb_ids 覆盖）。"""
    from src.models.wiki_page import WikiPage

    try:
        async with get_kb_lock(kb_id):
            rows = (
                await db.execute(select(WikiPage).filter(WikiPage.kb_id == str(kb_id)))
            ).scalars().all()
            if not rows:
                return
            for row in rows:
                await _delete_page_artifacts(row)
            await db.execute(sa_delete(WikiPage).filter(WikiPage.kb_id == str(kb_id)))
            await db.commit()
            logger.info(f"Wiki 级联（知识库删除）: kb={kb_id}, 清理页面 {len(rows)} 页")
    except Exception as e:
        logger.warning(f"Wiki 级联（知识库删除）失败（不影响删除主流程）: {e}")


async def _delete_page_artifacts(row) -> None:
    """删除单页的向量与 MinIO 正文；单项失败仅告警，继续其余清理。"""
    from src.services.minio_service import MinioService
    from src.services.vector_store import VectorStoreManager

    try:
        vector_store = await VectorStoreManager.get_instance()
        await vector_store.milvus_service.delete_by_document_id(row.id)
    except Exception as e:
        logger.warning(f"Wiki 页向量删除失败（继续）: page={row.id}: {e}")
    try:
        minio = await MinioService.get_instance()
        await minio.delete_file_async(row.content_path)
    except Exception as e:
        logger.warning(f"Wiki 页 MinIO 正文删除失败（继续）: {row.content_path}: {e}")
