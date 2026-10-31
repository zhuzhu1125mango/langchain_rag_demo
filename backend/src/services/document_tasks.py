"""文档后台任务与索引一致性（自 `api/document.py` 抽出，CODE-1 拆分）。

职责边界：
- 后台任务：`process_document_async`（解析 → 向量化 → 规则/LLM 分析 → Wiki 编译 → 发布）、
  `process_document_delete_async`（MinIO + 向量 + DB + Wiki 级联）；
- 一致性：`apply_document_status` / `delete_document_vectors` / `schedule_document_reindex`
  ——SEC-4「下架删向量、上架重建」的 fail-closed 实现；
- 文本工具：`analyze_document_with_llm`（单次 LLM 分类+质量）、`calculate_similarity`
  （词频余弦 + 字符 Jaccard，重复检测端点使用）。

约定：本模块不依赖 FastAPI 请求对象；`HTTPException` 仅用于把「索引不可用」
表达成 503（调用方在 API 层，语义与改造前一致）。
"""

import asyncio
import logging
import math
import os
import re
import time
import uuid
from collections import Counter
from typing import Dict, Optional

from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import select

from src.config import settings
from src.database import async_session_maker
from src.models import Document
from src.services.document_analyzer import DocumentAnalyzer
from src.services.document_processor import process_document
from src.services.notification_service import (
    notify_doc_list_changed,
    notify_task_completed,
    notify_task_failed,
    notify_task_progress,
)
from src.services.progress_manager import (
    create_upload_progress,
    update_upload_progress,
)
from src.services.rag_chain import RAGChain
from src.services.semantic_cache_service import (
    schedule_invalidation as _schedule_semantic_cache_invalidation,
)
from src.services.vector_store import VectorStoreManager
from src.utils.error_handling import ErrorSeverity, log_exception

logger = logging.getLogger("rag_system")

# 文档类型/领域 → 中文标签映射（DocumentAnalyzer 规则分析结果的展示名；
# 智能分析(LLM)与独立分类端点两处消费，避免硬编码漂移）
TYPE_LABELS = {
    "technical": "技术文档",
    "business": "业务文档",
    "report": "报告文档",
    "manual": "操作手册",
    "policy": "政策文件",
    "news": "新闻资讯",
    "other": "其他文档",
}
DOMAIN_LABELS = {
    "it": "信息技术",
    "finance": "金融",
    "healthcare": "医疗健康",
    "education": "教育",
    "legal": "法律",
    "government": "政府",
    "enterprise": "企业管理",
    "other": "其他领域",
}

# 索引重建任务引用集合：直接调用（非 BackgroundTasks）时避免任务被 GC 回收
_reindex_tasks: set = set()


# ---------------------------------------------------------------------------
# 索引 / 缓存一致性（SEC-4）
# ---------------------------------------------------------------------------
def invalidate_doc_cache(*kb_ids: Optional[str]) -> None:
    """文档状态变更后失效相关知识库的语义缓存（best-effort）。"""
    for kb_id in {str(k) for k in kb_ids if k}:
        try:
            _schedule_semantic_cache_invalidation(kb_id)
        except Exception as exc:
            log_exception(
                logger, exc,
                action="document.cache.invalidate_after_status",
                severity=ErrorSeverity.RECOVERABLE,
                message="文档状态变更后失效语义缓存失败",
                kb_id=kb_id,
            )


async def delete_document_vectors(doc_id: str) -> None:
    """删除文档全部向量；失败抛 503，调用方不得继续改状态（fail-closed）。"""
    try:
        vector_store = await VectorStoreManager.get_instance()
        await vector_store.delete_by_document_id(doc_id)
    except Exception as exc:
        # fail-closed：向量删不掉就不能改状态，否则会留下"已下架但仍被检索到"的脏数据
        log_exception(
            logger, exc,
            action="document.vectors.delete_by_document",
            message="删除文档向量失败，状态变更已中止",
            doc_id=doc_id,
        )
        raise HTTPException(
            status_code=503,
            detail="向量索引暂不可用，文档状态未变更，请稍后重试",
        )


async def _discard_partial_vectors(doc_id: str, *, action: str, reason: str) -> bool:
    """回滚「向量已写入、但文档最终判定失败」的残留切片（best-effort）。

    这类残留最隐蔽：文档列表显示「处理失败」，检索却能命中它的正文——用户以为
    内容没进库，实际已经进了，并且重试上传会再写一份，检索结果成对出现。
    清理失败按 REPORTABLE 记录（元数据已标失败，切片再也无人认领）。
    """
    try:
        vector_store = await VectorStoreManager.get_instance()
        await vector_store.delete_by_document_id(doc_id)
    except Exception as exc:
        log_exception(
            logger, exc,
            action=action,
            severity=ErrorSeverity.REPORTABLE,
            message="文档处理失败后清理残留向量失败：切片仍可被检索命中（文档已标 failed）",
            doc_id=doc_id,
            reason=reason,
        )
        return False
    logger.info("文档处理失败，已回滚残留向量: doc=%s reason=%s", doc_id, reason)
    return True


def doc_has_indexed_vectors(doc: Document) -> bool:
    """判断文档是否可能有向量：published，或已处理完成且 chunks_count > 0。"""
    return getattr(doc, "status", None) == "published" or (
        getattr(doc, "processing_status", None) == "completed"
        and (getattr(doc, "chunks_count", 0) or 0) > 0
    )


def schedule_document_reindex(
    doc: Document, background_tasks: Optional[BackgroundTasks]
) -> None:
    """提交后台重建索引任务（上架 / 移动 KB 后调用，调用前需先 commit）。"""
    if not doc.file_path:
        raise HTTPException(status_code=409, detail="文档原文件缺失，无法重建索引")

    task_id = f"reindex_{doc.id}_{uuid.uuid4().hex[:8]}"
    create_upload_progress(task_id, doc.filename, doc.size or 0, owner_id=doc.owner_id)
    update_upload_progress(task_id, status="processing", message="正在重建索引...")

    args = (
        str(doc.id),
        doc.file_path,
        str(doc.kb_id) if doc.kb_id else "",
        task_id,
        None,
        None,
    )
    if background_tasks is not None:
        background_tasks.add_task(process_document_async, *args)
        return

    # 直接调用（如单测/内部调用）时用受引用保护的后台任务，避免 GC 丢失。
    task = asyncio.create_task(process_document_async(*args))
    _reindex_tasks.add(task)
    task.add_done_callback(_reindex_tasks.discard)


async def recover_stale_processing_documents(
    stale_after_seconds: float = 900.0,
) -> Dict[str, int]:
    """启动时把「上次进程没跑完」的文档标记为失败（否则永远停在处理中）。

    文档状态机只有处理成功（completed）与明确异常（failed）两个出口：
    进程被强杀、OOM、`docker restart` 打断时两个都走不到，库里留下
    ``processing_status='processing'`` 的记录。前端会一直转圈，而且同一文件
    再次上传会撞上「内容重复」检测，用户既用不了也传不上去 —— 只有重启后
    的这条清理能把它解开。

    阈值用于多副本部署：别的 worker 正在处理的文档会随阶段推进不断刷新
    ``updated_at``，超时未刷新才认定已死。

    Args:
        stale_after_seconds: ``updated_at`` 超过该秒数未刷新才判定为僵死。

    Returns:
        ``{"recovered": 标记数量}``。
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import or_, update

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)
    recovered = 0
    try:
        async with async_session_maker() as db:
            result = await db.execute(
                update(Document)
                .where(Document.processing_status.in_(("processing", "uploading")))
                .where(or_(Document.updated_at.is_(None), Document.updated_at < cutoff))
                .values(
                    processing_status="failed",
                    processing_message="服务重启导致处理中断，请重新上传",
                    processing_progress=0,
                )
                .execution_options(synchronize_session=False)
            )
            await db.commit()
            recovered = int(result.rowcount or 0)
    except Exception as e:
        # 清理失败不阻断启动：留着「处理中」至少还能人工介入
        log_exception(
            logger,
            e,
            action="document.recover.stale",
            message="启动时回收僵死文档失败，部分文档可能仍停在「处理中」",
            stale_after_s=stale_after_seconds,
        )
        return {"recovered": 0}

    if recovered:
        log_exception(
            logger,
            RuntimeError("存在上次进程中断的文档处理"),
            action="document.recover.stale",
            severity=ErrorSeverity.REPORTABLE,
            message="启动时发现上次未完成的文档处理，已标记为失败（需用户重新上传）",
            recovered=recovered,
            stale_after_s=stale_after_seconds,
        )
    return {"recovered": recovered}


async def apply_document_status(
    db,
    doc: Document,
    new_status: str,
    background_tasks: Optional[BackgroundTasks],
) -> None:
    """应用状态变更，并同步向量索引与语义缓存。"""
    old_status = doc.status
    if new_status == old_status:
        return
    if getattr(doc, "processing_status", None) == "processing":
        # 处理/重建期间禁止切换状态，避免后台任务完成后把状态覆盖回去。
        raise HTTPException(status_code=409, detail="文档正在处理中，请稍后再试")

    if new_status != "published":
        # 下架/归档：先删向量，删除失败则保持原状态。
        if doc_has_indexed_vectors(doc):
            await delete_document_vectors(str(doc.id))
        doc.status = new_status
        doc.chunks_count = 0
        await db.commit()
        invalidate_doc_cache(str(doc.kb_id) if doc.kb_id else None)
        return

    # 上架：只有非 published 才需要重建。
    if old_status == "published":
        return

    # 存量异常：旧状态非 published 但可能残留向量，先清干净再重建。
    if doc_has_indexed_vectors(doc):
        await delete_document_vectors(str(doc.id))
    doc.processing_status = "processing"
    doc.processing_message = "正在重建索引..."
    doc.processing_progress = 0
    doc.chunks_count = 0
    await db.commit()
    schedule_document_reindex(doc, background_tasks)
    invalidate_doc_cache(str(doc.kb_id) if doc.kb_id else None)


# ---------------------------------------------------------------------------
# 后台任务
# ---------------------------------------------------------------------------
async def process_document_async(
    doc_id: str,
    file_path: str,
    kb_id: str,
    task_upload_id: str,
    chunk_size: Optional[int],
    chunk_overlap: Optional[int],
):
    """异步处理文档（后台任务）。

    在独立的事务中处理文档：解析 → 向量化 → 规则分析 → LLM 增强 → Wiki 编译 → 发布。
    """
    async with async_session_maker() as db:
        # 向量一旦写入，后续任何一步失败都会留下「文档标 failed、切片却可被检索到」
        # 的孤儿向量；重试上传还会再叠一份，导致检索结果重复。用这个标记把清理
        # 限定在「确实写过向量」之后，避免解析阶段就失败也白跑一次 Milvus 删除。
        vectors_written = False
        try:
            # 获取文档记录
            result = await db.execute(select(Document).filter(Document.id == uuid.UUID(doc_id)))
            doc = result.scalars().first()

            if not doc:
                await notify_task_failed(task_upload_id, "文档记录不存在")
                return

            # 阶段1：解析文档 (0-30%)
            doc.processing_message = "正在解析文档..."
            doc.processing_progress = 10
            await db.commit()
            await notify_task_progress(task_upload_id, 10, "正在解析文档...")

            # 解析含 MinIO 下载与 PDF/Office 解析（同步阻塞），移入线程池避免阻塞事件循环
            chunks = await asyncio.to_thread(process_document, file_path, chunk_size, chunk_overlap)

            # 为每个 chunk 补充 document_id 元数据，确保向量能正确关联到文档
            for chunk in chunks:
                chunk.metadata["document_id"] = doc_id

            doc.processing_progress = 30
            await db.commit()
            await notify_task_progress(task_upload_id, 30, f"文档解析完成，共{len(chunks)}个文本块")

            # 阶段2：向量化 (30-70%)
            doc.processing_message = "正在向量化..."
            doc.processing_progress = 40
            await db.commit()
            await notify_task_progress(task_upload_id, 40, "正在生成向量...")

            vector_store = await VectorStoreManager.get_instance()
            await vector_store.add_documents(chunks, kb_id)
            vectors_written = True

            doc.processing_progress = 70
            await db.commit()
            await notify_task_progress(task_upload_id, 70, "向量生成完成")

            # 阶段3：规则分析 (70-90%)
            doc.processing_message = "正在分析文档内容..."
            doc.processing_progress = 85
            await db.commit()
            await notify_task_progress(task_upload_id, 85, "正在分析文档内容...")

            # 提取文档完整内容进行规则分析
            full_content = "\n\n".join(
                chunk.page_content for chunk in chunks if hasattr(chunk, "page_content")
            )
            if not full_content:
                full_content = "\n\n".join(str(chunk) for chunk in chunks)

            # 规则分析：快速分类 + 质量评估
            document_type, topics, domain = DocumentAnalyzer.analyze_document_content(full_content)
            quality_result = DocumentAnalyzer.evaluate_quality(full_content)

            # 保存规则分析结果
            doc.document_type = document_type
            doc.document_type_label = TYPE_LABELS.get(document_type, "其他文档")
            doc.domain = domain
            doc.domain_label = DOMAIN_LABELS.get(domain, "其他领域")
            doc.topics = topics if topics else []
            doc.quality_score = int(quality_result.get("overall_score", 0))
            doc.quality_grade = quality_result.get("overall_grade", "")
            doc.quality_details = {
                "completeness": quality_result.get("completeness", 0),
                "readability": quality_result.get("readability", 0),
                "structure": quality_result.get("structure", 0),
                "relevance": quality_result.get("relevance", 0),
                "completeness_comment": quality_result.get("completeness_comment", ""),
                "readability_comment": quality_result.get("readability_comment", ""),
                "structure_comment": quality_result.get("structure_comment", ""),
                "relevance_comment": quality_result.get("relevance_comment", ""),
                "suggestions": quality_result.get("suggestions", []),
            }
            await db.commit()

            # 阶段5：LLM增强分析 (90-95%)
            doc.processing_message = "正在进行智能分析..."
            doc.processing_progress = 92
            await db.commit()
            await notify_task_progress(task_upload_id, 92, "正在进行智能分析...")

            try:
                # 智能分析：单次 LLM 调用同时完成「分类 + 质量评估」，92% 阶段约提速一半
                llm_result = await analyze_document_with_llm(full_content)
                if llm_result:
                    # 分类字段（LLM 成功则覆盖规则分析结果）
                    doc.document_type_label = llm_result.get("document_type", doc.document_type_label)
                    doc.topics = llm_result.get("topics", doc.topics)
                    doc.domain_label = llm_result.get("domain", doc.domain_label)
                    doc.summary = llm_result.get("summary", doc.summary)
                    # 质量字段（overall_grade 已在分析层推导；合并保留规则版独有键）
                    doc.quality_score = int(llm_result.get("overall_score", doc.quality_score))
                    doc.quality_grade = llm_result.get("overall_grade", doc.quality_grade)
                    doc.quality_details = {**(doc.quality_details or {}), **llm_result}

                await db.commit()
                logger.info(f"文档LLM智能分析完成: {doc.filename}")
            except Exception as e:
                # LLM 增强是可选增益，失败保留规则分析结果，不影响文档发布
                log_exception(
                    logger, e,
                    action="document.process.llm_analysis",
                    severity=ErrorSeverity.RECOVERABLE,
                    message="文档 LLM 智能分析失败，保留规则分析结果",
                    doc_id=doc_id,
                )

            # 阶段7：Wiki 编译（P2 LLM-Wiki 编译层，默认关闭）
            # 编译失败/超时仅记录日志与指标，不阻断文档上传（raw 层永远可用兜底）。
            # 去抖开启（DEBOUNCE_SECONDS>0）时编译完全移出上传管线。
            await _maybe_compile_wiki(db, doc, doc_id, kb_id, chunks, task_upload_id)

            # 阶段6：完成 (95-100%)
            doc.status = "published"
            doc.processing_status = "completed"
            doc.processing_message = "处理完成"
            doc.processing_progress = 100
            doc.chunks_count = len(chunks)
            await db.commit()

            # 进度记录写入终态（终态记录保留 60s 后由 progress_manager 自动清理）
            update_upload_progress(
                task_upload_id, status="completed", message="处理完成", processing_progress=100
            )

            # 发送完成通知
            await notify_task_completed(
                task_upload_id,
                {
                    "doc_id": doc_id,
                    "filename": doc.filename,
                    "chunks_count": len(chunks),
                },
            )

            # 通知文档列表已更新
            await notify_doc_list_changed(
                kb_id, doc_id, "created", owner_id=getattr(doc, "owner_id", None)
            )

            # P2-4 审计：文档上传完成（best-effort，后台任务用 owner_id 作操作者）
            from src.services.audit_service import record_audit

            owner_id = str(getattr(doc, "owner_id", "")) or None
            await record_audit(
                action="document.upload",
                resource_type="document",
                resource_id=str(doc_id),
                detail={"filename": doc.filename, "kb_id": str(kb_id)},
                actor_user_id=owner_id,
            )

            # 使知识库列表缓存失效，确保文档数量实时更新
            # （延迟导入：api 包在导入期会反向引入本模块，模块级导入会成环）
            try:
                from src.api.knowledge_base import invalidate_kb_list_cache

                await invalidate_kb_list_cache(doc.owner_id)
            except Exception as exc:
                log_exception(
                    logger, exc,
                    action="document.process.invalidate_kb_list_cache",
                    severity=ErrorSeverity.RECOVERABLE,
                    message="文档处理完成后刷新知识库列表缓存失败，计数可能短暂滞后",
                    doc_id=doc_id,
                )

            logger.info(f"文档处理完成: {doc.filename}, 共{len(chunks)}个文本块")

        except asyncio.CancelledError:
            # CancelledError 继承自 BaseException，不会走到下面的 except Exception：
            # 服务关闭 / 上层 wait_for 超时会在这里直接跳出，文档就此停在
            # processing_status="processing"，前端转圈到天荒地老，也没有任何日志。
            log_exception(
                logger,
                RuntimeError("文档处理任务被取消"),
                action="document.process.cancelled",
                severity=ErrorSeverity.REPORTABLE,
                message="文档处理被取消（服务关闭或任务超时），已标记为失败",
                doc_id=doc_id,
            )
            try:
                result = await db.execute(
                    select(Document).filter(Document.id == uuid.UUID(doc_id))
                )
                doc = result.scalars().first()
                if doc:
                    doc.processing_status = "failed"
                    doc.processing_message = "处理被中断，请重新上传"
                    doc.processing_progress = 0
                    await db.commit()
            except Exception as mark_exc:
                log_exception(
                    logger,
                    mark_exc,
                    action="document.process.mark_cancelled",
                    message="取消后写入失败状态失败，文档将停留在「处理中」",
                    doc_id=doc_id,
                )
            update_upload_progress(
                task_upload_id, status="failed", message="处理被中断，请重新上传"
            )
            if vectors_written:
                await _discard_partial_vectors(
                    doc_id, action="document.process.cancelled.vector_rollback", reason="cancelled"
                )
            raise

        except Exception as e:
            # 后台任务顶层：没有调用方兜底，这里是唯一记录点
            log_exception(
                logger, e,
                action="document.process.background",
                message="文档处理失败，已置为 failed 并通知前端",
                doc_id=doc_id,
            )

            # 更新文档状态为失败（用户可见文案保持通用，详细错误仅进日志）
            try:
                result = await db.execute(select(Document).filter(Document.id == uuid.UUID(doc_id)))
                doc = result.scalars().first()
                if doc:
                    doc.processing_status = "failed"
                    doc.processing_message = "处理失败，请重新上传或联系管理员"
                    doc.processing_progress = 0
                    await db.commit()
            except Exception as mark_exc:
                log_exception(
                    logger, mark_exc,
                    action="document.process.mark_failed",
                    message="写入文档失败状态时出错，前端可能一直显示处理中",
                    doc_id=doc_id,
                )

            # 进度记录写入终态
            update_upload_progress(task_upload_id, status="failed", message="处理失败，请稍后重试")

            # 向量写入发生在解析之后：此时失败必须回滚，否则「失败文档」仍被检索命中
            if vectors_written:
                await _discard_partial_vectors(
                    doc_id, action="document.process.failed.vector_rollback", reason="exception"
                )

            # 发送失败通知
            await notify_task_failed(task_upload_id, "文档处理失败")


async def _maybe_compile_wiki(db, doc, doc_id: str, kb_id: str, chunks, task_upload_id: str) -> None:
    """Wiki 编译阶段（去抖队列或同步编译），失败不阻断文档发布。"""
    if not settings.wiki_compile.WIKI_COMPILE_ENABLED:
        return

    debounce_seconds = max(0, settings.wiki_compile.WIKI_COMPILE_DEBOUNCE_SECONDS)
    if debounce_seconds > 0:
        from src.services.wiki_compile_scheduler import schedule_compile

        schedule_compile(kb_id, doc_id)
        await notify_task_progress(task_upload_id, 95, "已加入批量编译队列，将在后台执行")
        logger.info(
            f"Wiki 编译已加入去抖队列（{debounce_seconds}s 窗口合并）: "
            f"{doc.filename}, kb={kb_id}"
        )
        return

    doc.processing_message = "正在编译知识 Wiki..."
    await db.commit()
    await notify_task_progress(task_upload_id, 95, "正在编译知识 Wiki...")
    from src.middleware.prometheus import record_wiki_compile

    compile_started = time.monotonic()
    try:
        from src.services.wiki_compiler import WikiCompiler

        compile_result = await asyncio.wait_for(
            WikiCompiler().compile_document(db, kb_id, doc_id, chunks),
            timeout=settings.wiki_compile.WIKI_COMPILE_TIMEOUT_SECONDS,
        )
        record_wiki_compile(
            result="ok",
            pages_created=compile_result.pages_created,
            pages_updated=compile_result.pages_updated,
            duration=time.monotonic() - compile_started,
            fact_retention=compile_result.fact_retention_rate,
        )
        await notify_task_progress(
            task_upload_id,
            98,
            f"Wiki 编译完成：新建 {compile_result.pages_created} 页，"
            f"更新 {compile_result.pages_updated} 页",
        )
        logger.info(
            f"Wiki 编译完成: {doc.filename}, 新建 {compile_result.pages_created} 页, "
            f"更新 {compile_result.pages_updated} 页, 入库 {compile_result.chunks_indexed} 块"
        )
    except asyncio.TimeoutError:
        record_wiki_compile(result="timeout", duration=time.monotonic() - compile_started)
        logger.warning(
            f"Wiki 编译超时（不阻断文档上传）: {doc.filename}, "
            f"超时阈值 {settings.wiki_compile.WIKI_COMPILE_TIMEOUT_SECONDS}s"
        )
    except Exception as e:
        record_wiki_compile(result="failed")
        # Wiki 编译失败不阻断上传（raw 层永远可用兜底），但需要可观测
        log_exception(
            logger, e,
            action="document.wiki_compile",
            severity=ErrorSeverity.RECOVERABLE,
            message="Wiki 编译失败，不阻断文档上传",
            doc_id=doc_id,
        )


async def process_document_delete_async(doc_id: str, kb_id: str, file_path: str, task_id: str):
    """异步删除文档（后台任务）：向量 → DB 记录 → 原文件 → Wiki 级联。

    顺序刻意如此——三类数据的重要性不同：
    - 向量决定「还能不能被检索到」，DB 行决定「还能不能被用户发现/重试」，
      原文件只是附件；
    - 先删向量：失败则文档原样保留，用户重试即可，**不会**出现「文件已删、
      记录还在、内容还能被检索命中」的半删除态（旧顺序先删文件，正是这个坑）；
    - 再删 DB 行：失败则文档仍在列表但已搜不到，重试删除即可修复；
    - 最后删文件且 best-effort：文件残留只是存储占用，不影响删除语义。

    ``phase`` 让失败日志能指出「卡在哪一步」，否则只剩一句「删除失败」，
    运维无法判断该重试还是该手工清 Milvus。
    """
    phase = "vectors"
    async with async_session_maker() as db:
        try:
            # 阶段1：删除向量 (0-50%)
            await notify_task_progress(task_id, 10, "正在删除向量...")
            vector_store = await VectorStoreManager.get_instance()
            await vector_store.delete_by_document_id(doc_id)
            await notify_task_progress(task_id, 50, "向量已删除")

            # 阶段2：删除数据库记录 (50-70%)
            phase = "database"
            result = await db.execute(select(Document).filter(Document.id == uuid.UUID(doc_id)))
            doc = result.scalars().first()
            owner_id = doc.owner_id if doc else None
            if doc:
                await db.delete(doc)
                await db.commit()
            else:
                # 并发删除 / 上次已删一半：不是错误，但必须留痕，否则「删了没删」无从判断
                logger.info("删除文档时记录已不存在，继续清理文件: doc=%s", doc_id)
            await notify_task_progress(task_id, 70, "记录已删除")

            # 阶段3：删除原文件 (70-90%)，失败只告警
            phase = "object"
            await notify_task_progress(task_id, 80, "正在清理文件...")
            try:
                if file_path.startswith("minio://"):
                    from src.services.minio_service import MinioService

                    minio_service = await MinioService.get_instance()
                    await minio_service.delete_file_async(file_path)
                elif os.path.exists(file_path):
                    os.remove(file_path)
            except Exception as exc:
                log_exception(
                    logger, exc,
                    action="document.delete.object",
                    severity=ErrorSeverity.RECOVERABLE,
                    message="文档记录与向量已删除，仅原文件清理失败（对象存储残留，不影响检索）",
                    doc_id=doc_id,
                    file_path=file_path,
                )
            await notify_task_progress(task_id, 90, "文件清理完成")

            await notify_task_progress(task_id, 100, "删除完成")

            # 进度记录写入终态（终态记录保留 60s 后由 progress_manager 自动清理）
            update_upload_progress(task_id, status="completed", message="删除完成")

            # 发送完成通知
            await notify_task_completed(task_id, {"doc_id": doc_id, "status": "deleted"})

            # 通知文档列表已更新
            await notify_doc_list_changed(kb_id, doc_id, "deleted", owner_id=owner_id)

            # 使知识库列表缓存失效，确保文档数量实时更新（延迟导入，见上）
            if owner_id:
                try:
                    from src.api.knowledge_base import invalidate_kb_list_cache

                    await invalidate_kb_list_cache(owner_id)
                except Exception as exc:
                    log_exception(
                        logger, exc,
                        action="document.delete.invalidate_kb_list_cache",
                        severity=ErrorSeverity.RECOVERABLE,
                        message="文档删除完成后刷新知识库列表缓存失败，计数可能短暂滞后",
                        doc_id=doc_id,
                    )

            # P1-3：知识库内容已变更，失效相关语义缓存
            _schedule_semantic_cache_invalidation(kb_id)

            # P2：Wiki 级联清理（剪源/删孤儿页）。放在最后且单独兜底：
            # 文档本身已经删干净了，级联失败不应把整次删除改判为 failed。
            phase = "wiki_cascade"
            try:
                from src.services.wiki_cascade import on_document_deleted

                await on_document_deleted(db, kb_id, doc_id)
            except Exception as exc:
                log_exception(
                    logger, exc,
                    action="document.delete.wiki_cascade",
                    severity=ErrorSeverity.REPORTABLE,
                    message="文档已删除，但 Wiki 级联清理失败（页面可能仍引用已删来源）",
                    doc_id=doc_id,
                    kb_id=kb_id,
                )

            logger.info(f"文档删除完成: {doc_id}")

        except Exception as e:
            # 后台任务顶层：删除链路没有调用方兜底。
            # phase 必须进日志与前端文案：卡在向量/记录/文件 三处的处置完全不同。
            log_exception(
                logger, e,
                action="document.delete.background",
                message=f"文档删除失败（阶段: {phase}）",
                doc_id=doc_id,
                phase=phase,
            )
            update_upload_progress(
                task_id, status="failed", message=f"删除失败（{phase}），请稍后重试"
            )
            await notify_task_failed(task_id, "文档删除失败")


# ---------------------------------------------------------------------------
# 文本工具
# ---------------------------------------------------------------------------
async def analyze_document_with_llm(content: str) -> dict:
    """使用 LLM 对文档做一次「分类 + 质量评估」智能分析（委托给 RAGChain）。

    失败（解析失败/异常兜底）时返回 None，调用方保留规则分析结果。
    """
    vector_store = await VectorStoreManager.get_instance()
    rag_chain = await RAGChain.get_instance(vector_store)
    result = await rag_chain.analyze_document(content)
    if result.get("summary") in ("无法解析评估结果", "评估失败", "无法解析文档内容", "分类失败"):
        return None
    return result


def calculate_similarity(text1: str, text2: str) -> float:
    """计算两段文本相似度（词频余弦 + 字符 Jaccard）。

    注意：这是重复检测端点使用的轻量规则版实现（词频/字符级），
    与 ``DocumentAnalyzer.detect_duplicates`` 的语义向量相似度用途互补。
    """
    text1 = text1.lower()
    text2 = text2.lower()

    # 分词并统计词频
    words1 = Counter(re.findall(r"\w+", text1))
    words2 = Counter(re.findall(r"\w+", text2))

    if not words1 or not words2:
        return 0.0

    # 计算余弦相似度
    intersection = set(words1.keys()) & set(words2.keys())
    if not intersection:
        cosine = 0.0
    else:
        dot_product = sum(words1[w] * words2[w] for w in intersection)
        magnitude1 = math.sqrt(sum(v**2 for v in words1.values()))
        magnitude2 = math.sqrt(sum(v**2 for v in words2.values()))
        cosine = dot_product / (magnitude1 * magnitude2) if magnitude1 and magnitude2 else 0.0

    # 字符级 Jaccard 作为补充
    chars1 = set(text1)
    chars2 = set(text2)
    char_jaccard = len(chars1 & chars2) / len(chars1 | chars2) if chars1 | chars2 else 0.0

    return (cosine + char_jaccard) / 2
