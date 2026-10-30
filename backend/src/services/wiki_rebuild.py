"""Wiki 全量重编译服务（Phase 2）：自 scripts/rebuild_wiki.py 下沉，供脚本与 API 复用。

流程：
1. 删除该 KB 全部既有 Wiki 产物（wiki_pages 表行 + MinIO 正文 + 向量库 wiki 切片）
2. 从 Milvus 读取各文档的原始 chunks（raw 层不可变，是重编译的事实来源）
3. 逐文档调用 WikiCompiler 重新编译（受按 KB 串行锁约束）

不受 WIKI_COMPILE_ENABLED 约束（该开关仅控制上传管线自动编译）。
"""

import inspect
import logging
import time
from typing import Callable, List, Optional, Union

from sqlalchemy import select

from src.models import Document
from src.utils.error_handling import ErrorSeverity, log_exception

logger = logging.getLogger("wiki_rebuild")

# 进度回调：sync 可调用或 async 协程函数
ProgressCallback = Callable[[int, str], Union[None, "object"]]


async def load_raw_chunks(kb_id: str, doc_id: str):
    """从 Milvus 拉取某文档的 raw chunks，按 chunk_index 排序构造 Document 列表。"""
    from langchain_core.documents import Document

    from src.services.vector_store import VectorStoreManager

    vector_store = await VectorStoreManager.get_instance()
    results = await vector_store.milvus_service.get_document_chunks(str(doc_id))
    results = [r for r in results if r.get("source_kind", "raw") == "raw"]
    results.sort(key=lambda r: r.get("chunk_index", 0))
    return [
        Document(
            page_content=r.get("content", ""),
            metadata={
                "document_id": str(doc_id),
                "source": r.get("source", ""),
                "chunk_index": r.get("chunk_index", 0),
                "heading_path": r.get("heading_path", ""),
            },
        )
        for r in results
    ]


async def _wipe_existing(db, kb_id: str) -> int:
    """删除该 KB 的 Wiki 页面（DB 行 + MinIO 正文 + 向量切片），返回删除页数。"""
    from sqlalchemy import delete as sa_delete

    from src.models.wiki_page import WikiPage
    from src.services.minio_service import MinioService
    from src.services.vector_store import VectorStoreManager

    rows = (await db.execute(select(WikiPage).filter(WikiPage.kb_id == str(kb_id)))).scalars().all()
    if not rows:
        return 0

    minio = await MinioService.get_instance()
    vector_store = await VectorStoreManager.get_instance()
    for row in rows:
        await vector_store.milvus_service.delete_by_document_id(row.id)
        try:
            await minio.delete_file_async(row.content_path)
        except Exception as e:
            log_exception(
                logger, e,
                action="wiki.rebuild.delete_object",
                severity=ErrorSeverity.RECOVERABLE,
                message="MinIO 对象删除失败，继续清理其余产物",
                kb_id=str(kb_id), page_id=str(row.id),
            )
    await db.execute(sa_delete(WikiPage).filter(WikiPage.kb_id == str(kb_id)))
    await db.commit()
    return len(rows)


async def rebuild_kb_wiki(
    db,
    kb_id: str,
    progress_cb: Optional[ProgressCallback] = None,
) -> dict:
    """按 KB 全量重编译；progress_cb(percent, message) 可选（sync 或 async 均可）。

    返回 {"documents", "pages_wiped", "pages_created", "pages_updated", "chunks_indexed",
    "documents_failed", "failed_documents"}。

    `documents_failed > 0` 表示该 KB 的 Wiki 处于缺页状态：本流程先清后建，
    失败文档的旧页面已删且未重建，调用方必须据此改汇报口径。
    """

    async def report(percent: int, message: str) -> None:
        if progress_cb is None:
            return
        try:
            result = progress_cb(percent, message)
            if inspect.isawaitable(result):
                await result
        except Exception as e:
            log_exception(
                logger, e,
                action="wiki.rebuild.progress_callback",
                severity=ErrorSeverity.RECOVERABLE,
                message="Wiki 重编译进度回调失败（不影响编译本身）",
            )

    from src.middleware.prometheus import record_wiki_compile
    from src.services.wiki_compiler import WikiCompiler

    docs = (
        await db.execute(
            select(Document).filter(
                Document.kb_id == kb_id, Document.status == "published"
            )
        )
    ).scalars().all()
    await report(5, f"待编译文档 {len(docs)} 篇")

    wiped = await _wipe_existing(db, kb_id)
    await report(10, f"已清理既有 Wiki 页面 {wiped} 页")

    compiler = WikiCompiler()
    total_created = total_updated = total_indexed = 0
    failed_docs: List[str] = []
    span = 90 / len(docs) if docs else 0
    for i, doc in enumerate(docs, 1):
        chunks = await load_raw_chunks(kb_id, doc.id)
        if not chunks:
            await report(10 + int(span * i), f"[{i}/{len(docs)}] {doc.filename}: 无 raw chunks，跳过")
            continue
        try:
            compile_started = time.monotonic()
            result = await compiler.compile_document(db, str(kb_id), [str(doc.id)], chunks)
            record_wiki_compile(
                result="ok",
                pages_created=result.pages_created,
                pages_updated=result.pages_updated,
                duration=time.monotonic() - compile_started,
                fact_retention=result.fact_retention_rate,
            )
            total_created += result.pages_created
            total_updated += result.pages_updated
            total_indexed += result.chunks_indexed
            await report(
                10 + int(span * i),
                f"[{i}/{len(docs)}] {doc.filename}: 新建 {result.pages_created} 页，"
                f"更新 {result.pages_updated} 页",
            )
        except Exception as e:
            record_wiki_compile(result="failed")
            # 单文档失败会以「部分成功」形态返回，必须能定位到具体文档
            log_exception(
                logger, e,
                action="wiki.rebuild.compile_document",
                message="Wiki 重编译失败，跳过该文档",
                kb_id=str(kb_id), doc_id=str(doc.id), filename=doc.filename,
            )
            failed_docs.append(doc.filename or str(doc.id))
            await report(10 + int(span * i), f"[{i}/{len(docs)}] {doc.filename}: 编译失败，跳过")

    if failed_docs:
        # 本流程是「先清后建」：失败文档的旧页面已被 _wipe_existing 删掉且不会再生成，
        # 所以这个 KB 的 Wiki 实际处于「缺页」状态。逐文档的日志只能说明单篇失败，
        # 看不出「整个知识库现在少了哪几块」，必须有一条聚合记录 + 返回值标记。
        all_failed = len(failed_docs) >= len(docs)
        log_exception(
            logger,
            RuntimeError("Wiki 重编译存在失败文档"),
            action="wiki.rebuild.incomplete",
            severity=ErrorSeverity.REPORTABLE,
            message=(
                "Wiki 全量重编译全部失败，该知识库的 Wiki 已被清空"
                if all_failed
                else "部分文档重编译失败，其 Wiki 页面已随清理删除且未重建"
            ),
            kb_id=str(kb_id),
            failed=len(failed_docs),
            total=len(docs),
            samples=failed_docs[:5],
        )

    return {
        "documents": len(docs),
        "pages_wiped": wiped,
        "pages_created": total_created,
        "pages_updated": total_updated,
        "chunks_indexed": total_indexed,
        "documents_failed": len(failed_docs),
        "failed_documents": failed_docs[:10],
    }
