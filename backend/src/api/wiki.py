"""知识库 Wiki 页面 API（LLM-Wiki 编译层 Phase 2）。

提供编译产物（实体页/主题页/索引页）的查看与全量重编译：
- GET  /knowledge_bases/{kb_id}/wiki/pages            页面列表
- GET  /knowledge_bases/{kb_id}/wiki/pages/{pid}/content  单页正文预览
- POST /knowledge_bases/{kb_id}/wiki/rebuild          全量重编译（后台任务）

权限沿用 KB 归属校验（require_owner）。
"""

import logging
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import CurrentUser, get_current_user, require_owner
from src.database import get_db
from src.models import KnowledgeBase
from src.models.wiki_page import WikiPage

logger = logging.getLogger("wiki_api")

router = APIRouter(prefix="/knowledge_bases/{kb_id}/wiki", tags=["wiki"])


# ----------------------------------------------------------------------
# 响应模型
# ----------------------------------------------------------------------
class WikiPageSummary(BaseModel):
    id: str
    page_type: str
    title: str
    revision: int
    status: str
    source_doc_count: int
    updated_at: Optional[datetime] = None


class WikiPageListResponse(BaseModel):
    pages: List[WikiPageSummary]


class WikiPageContentResponse(BaseModel):
    id: str
    title: str
    page_type: str
    content: str


class WikiRebuildResponse(BaseModel):
    upload_id: str
    message: str


# ----------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------
async def _get_owned_kb(db: AsyncSession, kb_id: str, current_user: CurrentUser) -> KnowledgeBase:
    """校验 KB 存在且属于当前用户。"""
    try:
        kb_uuid = uuid.UUID(kb_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的知识库ID")
    kb = (
        await db.execute(select(KnowledgeBase).filter(KnowledgeBase.id == kb_uuid))
    ).scalar_one_or_none()
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    require_owner(kb.owner_id, current_user)
    return kb


async def _run_wiki_rebuild(kb_id: str, task_id: str) -> None:
    """后台任务：全量重编译（独立会话），进度经现有任务通道推送。"""
    from src.database import async_session_maker
    from src.services.notification_service import (
        notify_task_completed,
        notify_task_failed,
        notify_task_progress,
    )
    from src.services.progress_manager import update_upload_progress
    from src.services.wiki_rebuild import rebuild_kb_wiki

    async def progress(percent: int, message: str) -> None:
        await notify_task_progress(task_id, percent, message)
        # 同步写入 progress_store：复用 /upload/progress/ws/{id} 通道推送中间进度
        update_upload_progress(task_id, processing_progress=percent, message=message)

    async with async_session_maker() as db:
        try:
            stats = await rebuild_kb_wiki(db, kb_id, progress_cb=progress)
            message = (
                f"重编译完成: 新建 {stats['pages_created']} 页，"
                f"更新 {stats['pages_updated']} 页，入库 {stats['chunks_indexed']} 块"
            )
            await notify_task_progress(task_id, 100, message)
            update_upload_progress(task_id, status="completed", message=message)
            await notify_task_completed(task_id, {"kb_id": kb_id, "status": "wiki_rebuilt", **stats})
        except Exception as e:
            logger.error(f"Wiki 全量重编译失败: kb={kb_id}: {e}", exc_info=True)
            update_upload_progress(task_id, status="failed", message="Wiki 全量重编译失败")
            await notify_task_failed(task_id, "Wiki 全量重编译失败")


# ----------------------------------------------------------------------
# 端点
# ----------------------------------------------------------------------
@router.get("/pages", response_model=WikiPageListResponse)
async def list_wiki_pages(
    kb_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """页面列表（实体/主题/索引全量，含来源文档数）。"""
    kb = await _get_owned_kb(db, kb_id, current_user)
    rows = (
        await db.execute(
            select(WikiPage)
            .filter(WikiPage.kb_id == str(kb.id))
            .order_by(WikiPage.page_type, WikiPage.title)
        )
    ).scalars().all()
    return WikiPageListResponse(
        pages=[
            WikiPageSummary(
                id=row.id,
                page_type=row.page_type,
                title=row.title,
                revision=row.revision or 1,
                status=row.status,
                source_doc_count=len(row.source_doc_ids or []),
                updated_at=row.updated_at,
            )
            for row in rows
        ]
    )


@router.get("/pages/{page_id}/content", response_model=WikiPageContentResponse)
async def get_wiki_page_content(
    kb_id: str,
    page_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """单页正文预览（MinIO 读取，缺失返回空内容）。"""
    kb = await _get_owned_kb(db, kb_id, current_user)
    row = (
        await db.execute(
            select(WikiPage).filter(WikiPage.id == page_id, WikiPage.kb_id == str(kb.id))
        )
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Wiki 页面不存在")

    from src.services.minio_service import MinioService

    content = ""
    try:
        minio = await MinioService.get_instance()
        content = await minio.download_text_async(row.content_path) or ""
    except Exception as e:
        logger.warning(f"Wiki 页正文读取失败: page={row.id}: {e}")
    return WikiPageContentResponse(
        id=row.id, title=row.title, page_type=row.page_type, content=content
    )


@router.post("/rebuild", response_model=WikiRebuildResponse)
async def rebuild_wiki(
    kb_id: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """全量重编译（后台任务）：清既有 Wiki 产物 → 读 raw chunks → 逐文档重编译。"""
    kb = await _get_owned_kb(db, kb_id, current_user)
    task_id = f"wiki_rebuild_{kb.id}_{uuid.uuid4().hex[:8]}"

    from src.services.progress_manager import create_upload_progress, update_upload_progress

    create_upload_progress(task_id, "Wiki 全量重编译", 0)
    update_upload_progress(task_id, status="processing", message="正在提交重编译任务...")
    background_tasks.add_task(_run_wiki_rebuild, str(kb.id), task_id)

    return WikiRebuildResponse(upload_id=task_id, message="Wiki 全量重编译任务已提交")
