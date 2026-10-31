"""Wiki 级联清理（Phase 2，P5 增加级联重写）：源文档删除 / 知识库删除联动。

设计约定（llm-wiki-compile.md §9.2 / §14.2）：
- 失败不阻断删除主流程（对齐语义缓存失效钩子风格），内部捕获并记日志
- 孤儿页（失去全部来源支撑）直接删除：向量库 + MinIO + DB 三处清理
- 与编译共用 kb_wiki_lock 双层锁，避免与进行中的编译互相踩踏
- P5 WIKI_CASCADE_REWRITE 开启时：剪源后仍有剩余来源的页面由后台任务
  从剩余来源 raw chunks 重推导整页（失败保留当前内容）
"""

import asyncio
import logging
from typing import List

from sqlalchemy import delete as sa_delete
from sqlalchemy import select

from src.services.wiki_lock import get_kb_lock
from src.utils.error_handling import ErrorSeverity, log_exception

logger = logging.getLogger("wiki_cascade")


async def on_document_deleted(db, kb_id: str, doc_id: str) -> None:
    """源文档删除后剪除 wiki_pages 来源；孤儿页整体删除并重建索引页。

    WIKI_CASCADE_REWRITE 开启时，仍有剩余来源的受影响页面交给后台任务重写
    （独立会话 + 锁，失败保留当前内容，不影响删除主流程）。
    """
    from src.models.wiki_page import WikiPage
    from src.services.wiki_compiler import WikiCompiler

    try:
        affected_with_remaining: List[dict] = []
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
                    affected_with_remaining.append(
                        {"page_id": row.id, "title": row.title, "remaining": list(remaining)}
                    )
                    continue
                # 孤儿页：无任何来源支撑，直接删除
                await _delete_page_artifacts(row)
                await db.delete(row)
                removed_pages += 1

            await db.commit()
            logger.info(
                f"Wiki 级联（源文档删除）: kb={kb_id}, doc={doc_id}, "
                f"受影响 {len(rows)} 页，删孤儿页 {removed_pages}，"
                f"待重写 {len(affected_with_remaining)} 页"
            )
            if removed_pages:
                await WikiCompiler().rebuild_index_page(db, kb_id)
    except Exception as e:
        # 级联清理是删除主流程的附加动作，失败不阻断文档删除
        log_exception(
            logger, e,
            action="wiki_cascade.on_document_deleted",
            severity=ErrorSeverity.RECOVERABLE,
            message="Wiki 级联（源文档删除）失败，不影响删除主流程",
            kb_id=kb_id,
            doc_id=doc_id,
        )
        return

    # P5：级联重写（锁外调度后台任务；开关关闭时行为与 Phase 2 完全一致）
    if affected_with_remaining and _rewrite_enabled():
        _schedule_rewrite(kb_id, affected_with_remaining)


def _rewrite_enabled() -> bool:
    from src.config import settings

    return bool(settings.wiki_compile.WIKI_CASCADE_REWRITE)


def _schedule_rewrite(kb_id: str, affected: List[dict]) -> None:
    """创建后台重写任务（fire-and-forget，全量兜底防未消费异常告警）。"""
    task = asyncio.create_task(_rewrite_pages(kb_id, affected))
    _rewrite_tasks.add(task)
    task.add_done_callback(_rewrite_tasks.discard)


# 持有任务引用防止被 GC（done_callback 自动清理）
_rewrite_tasks = set()


async def _rewrite_pages(kb_id: str, affected: List[dict]) -> None:
    """P5 后台重写受影响页面：从剩余来源 raw chunks 重推导整页。

    逐页独立兜底：单页失败保留当前内容继续其余页面；剩余来源无有效材料的
    页面按孤儿页语义删除。全程持 KB 锁，与编译/再次级联互斥。
    """
    from src.models.wiki_page import WikiPage
    from src.services.minio_service import MinioService
    from src.services.wiki_compiler import CompiledPage, WikiCompiler
    from src.services.wiki_lock import get_kb_lock
    from src.services.wiki_rebuild import load_raw_chunks

    from src.database import async_session_maker

    rewritten = removed = 0
    try:
        async with async_session_maker() as db:
            compiler = WikiCompiler()
            async with get_kb_lock(kb_id):
                minio = await MinioService.get_instance()
                for item in affected:
                    try:
                        row = (
                            await db.execute(
                                select(WikiPage).filter(
                                    WikiPage.id == str(item["page_id"]),
                                    WikiPage.status == "active",
                                )
                            )
                        ).scalars().first()
                        if row is None:
                            continue

                        chunks = []
                        for d in item["remaining"]:
                            try:
                                chunks.extend(await load_raw_chunks(kb_id, d))
                            except Exception as e:
                                log_exception(
                                    logger, e,
                                    action="wiki_cascade.rewrite.load_source",
                                    severity=ErrorSeverity.RECOVERABLE,
                                    message="级联重写拉取剩余来源失败，跳过该来源",
                                    kb_id=kb_id,
                                    doc_id=d,
                                )
                        material = "\n\n".join(
                            c.page_content for c in chunks if getattr(c, "page_content", "")
                        )

                        if not material.strip():
                            # 剩余来源无有效材料：按孤儿页删除
                            await _delete_page_artifacts(row)
                            await db.delete(row)
                            await db.commit()
                            removed += 1
                            logger.info(
                                f"Wiki 级联重写：剩余来源无有效材料，删除页面 "
                                f"「{row.title}」(kb={kb_id})"
                            )
                            continue

                        content = await compiler.regenerate_page(
                            row.title, row.page_type, material
                        )
                        row.revision = (row.revision or 1) + 1
                        await minio.upload_text_async(row.content_path, content)
                        await db.commit()
                        # 旧向量删除后重入库 + links 重提取（失败仅告警不回滚正文）
                        await compiler._index_page(
                            kb_id,
                            row,
                            CompiledPage(title=row.title, page_type=row.page_type, content=content),
                            delete_old=True,
                        )
                        await compiler._fill_links(db, kb_id, [row], [content])
                        rewritten += 1
                    except Exception as e:
                        log_exception(
                            logger, e,
                            action="wiki_cascade.rewrite.page",
                            severity=ErrorSeverity.RECOVERABLE,
                            message="级联重写单页失败，保留当前内容继续其余页面",
                            kb_id=kb_id,
                            page_id=item.get("page_id"),
                        )
                if rewritten or removed:
                    await compiler.rebuild_index_page(db, kb_id)
        logger.info(
            f"Wiki 级联重写完成: kb={kb_id}, 重写 {rewritten} 页, 删除 {removed} 页"
        )
    except Exception as e:
        # fire-and-forget 任务顶层：无调用方兜底，整任务崩溃需要堆栈
        log_exception(
            logger, e,
            action="wiki_cascade.rewrite.task",
            message="Wiki 级联重写任务整体异常",
            kb_id=kb_id,
            pages=len(affected),
        )


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
        # 知识库已删，这些 wiki_pages 行与正文再无人认领；失败的页面级向量删除
        # 也已无处追溯，因此按 REPORTABLE 记录并带上库 ID 供人工清理。
        log_exception(
            logger, e,
            action="wiki_cascade.on_kb_deleted",
            severity=ErrorSeverity.REPORTABLE,
            message="Wiki 级联（知识库删除）失败：页面行/向量可能残留，需按 kb_id 人工清理",
            kb_id=str(kb_id),
        )


async def _delete_page_artifacts(row) -> None:
    """删除单页的向量与 MinIO 正文；单项失败仅告警，继续其余清理。"""
    from src.services.minio_service import MinioService
    from src.services.vector_store import VectorStoreManager

    try:
        vector_store = await VectorStoreManager.get_instance()
        await vector_store.milvus_service.delete_by_document_id(row.id)
    except Exception as e:
        # 向量删不掉 = 页面从列表消失、检索却仍能命中它的正文（引用指向已删页）。
        # 这是永久性残留，不是上游抖动，按 REPORTABLE 记，才能被人捡起来清理。
        log_exception(
            logger, e,
            action="wiki_cascade.delete_page_vectors",
            severity=ErrorSeverity.REPORTABLE,
            message="Wiki 页已删除但向量残留：该页内容仍可被检索命中",
            page_id=row.id,
            kb_id=getattr(row, "kb_id", None),
        )
    try:
        minio = await MinioService.get_instance()
        await minio.delete_file_async(row.content_path)
    except Exception as e:
        log_exception(
            logger, e,
            action="wiki_cascade.delete_page_file",
            severity=ErrorSeverity.RECOVERABLE,
            message="Wiki 页 MinIO 正文删除失败，继续其余清理",
            page_id=row.id,
        )
