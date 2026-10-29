"""文档上传路由：单文件 / 批量 / 分块配置 / 进度查询与 WS 推送。"""

import asyncio
import logging
import os
import uuid
from typing import List, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.knowledge_base import invalidate_kb_list_cache
from src.auth import CurrentUser, get_current_user, get_current_user_for_ws
from src.config import settings
from src.database import async_session_maker, get_db
from src.models import Document
from src.services.document_processor import SUPPORTED_EXTENSIONS
from src.services.document_tasks import process_document_async
from src.services.notification_service import notify_doc_list_changed, notify_task_failed
from src.services.progress_manager import (
    create_upload_progress,
    get_upload_progress,
    register_ws_connection,
    unregister_ws_connection,
    update_upload_progress,
)
from src.utils.error_handling import ErrorSeverity, log_exception
from src.utils.quota import consume_upload_quota, refund_upload_quota

from .common import ensure_upload_size as _ensure_upload_size, resolve_target_kb as _resolve_target_kb

logger = logging.getLogger("rag_system")

router = APIRouter(tags=["documents"])
# WebSocket 端点专用 router：不能挂 router 级 HTTP 认证依赖
# （APIKeyHeader 在 WS 上下文中缺少 request 对象会抛 TypeError），
# 认证在各 WS 端点内通过 get_current_user_for_ws 完成。
ws_router = APIRouter(tags=["documents"])


@router.post("/upload")
async def upload_document(
    file: UploadFile = File(...),
    kb_id: Optional[str] = Query(None, description="知识库ID，不传则使用默认知识库"),
    chunk_size: Optional[int] = Query(None, ge=50, le=2000, description="文本块大小（50-2000）"),
    chunk_overlap: Optional[int] = Query(None, ge=0, le=500, description="文本块重叠大小（0-500）"),
    upload_id: Optional[str] = Query(None, description="上传任务ID（用于WebSocket进度推送）"),
    background_tasks: BackgroundTasks = None,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    上传文档（异步处理）

    此接口会：
    1. 立即返回任务ID和文档ID
    2. 在后台异步处理文档（解析、向量化）
    3. 通过WebSocket实时推送处理进度
    """
    _, ext = os.path.splitext(file.filename)
    if ext.lower() not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型: {ext}")

    # 大小上限校验（实测字节数，不信任客户端声明）
    actual_size = _ensure_upload_size(file)

    # 验证知识库 ID（显式 kb_id 校验归属；缺省回退用户默认知识库）
    kb = await _resolve_target_kb(db, kb_id, current_user)
    kb_uuid = kb.id

    # SEC-2：上传日配额（按用户字节数；超限直接 429，不落 MinIO）
    if not await consume_upload_quota(current_user.user_id, actual_size):
        raise HTTPException(
            status_code=429,
            detail="上传日配额已用尽，请明日再试或联系管理员",
            headers={"Retry-After": "3600"},
        )
    quota_consumed = True

    # 生成上传ID（如果没有提供）
    task_upload_id = upload_id if upload_id else str(uuid.uuid4())
    doc_id = uuid.uuid4()

    try:
        from src.services.minio_service import MinioService
        minio_service = await MinioService.get_instance()

        # 更新状态：开始上传
        create_upload_progress(task_upload_id, file.filename, actual_size, owner_id=current_user.user_id)
        update_upload_progress(task_upload_id, status="uploading", message="正在上传文件...")

        # 先创建文档记录，标记为上传中
        doc = Document(
            id=doc_id,
            filename=file.filename,
            file_path="",
            file_type=ext.lower(),
            size=actual_size,
            kb_id=kb_uuid,
            owner_id=current_user.user_id,
            status="draft",
            processing_status="uploading",
            processing_message="正在上传文件...",
            processing_progress=0,
            chunks_count=0,
            tags=[]
        )
        db.add(doc)
        await db.commit()

        # MinIO 上传可达数十秒（大文件）：先释放请求级 DB 连接再上传，
        # 避免 session 横跨网络 I/O 占死连接池；后续更新改用独立短事务。
        await db.close()

        # 上传文件到 MinIO（异步变体：网络 I/O 不阻塞事件循环）
        file_path = await minio_service.upload_file_async(file, doc_id)

        # 独立短事务更新文档记录（请求 session 已在上传前释放）
        async with async_session_maker() as _s:
            result = await _s.execute(select(Document).filter(Document.id == doc_id))
            doc = result.scalars().first()
            if doc is not None:
                doc.file_path = file_path
                doc.processing_status = "processing"
                doc.processing_message = "正在处理文档..."
                doc.processing_progress = 5
                await _s.commit()

        # 立即返回，后台处理
        update_upload_progress(task_upload_id, status="processing", message="正在处理文档...")

        # 将文档处理添加到后台任务
        background_tasks.add_task(
            process_document_async,
            str(doc_id),
            file_path,
            str(kb_uuid),
            task_upload_id,
            chunk_size,
            chunk_overlap
        )

        # 通知文档列表即将更新
        await notify_doc_list_changed(
            str(kb_uuid), str(doc_id), "created", owner_id=current_user.user_id
        )

        # 使知识库列表缓存失效，确保文档数量实时更新
        try:
            await invalidate_kb_list_cache(current_user.user_id)
        except Exception as exc:
            log_exception(
                logger, exc,
                action="document.upload.invalidate_kb_list_cache",
                severity=ErrorSeverity.RECOVERABLE,
                message="文档上传后刷新知识库列表缓存失败，计数可能短暂滞后",
            )

        return {
            "id": str(doc_id),
            "message": "文件上传成功，正在后台处理",
            "upload_id": task_upload_id,
            "status": "processing"
        }

    except HTTPException:
        if quota_consumed:
            await refund_upload_quota(current_user.user_id, actual_size)
        raise
    except Exception as e:
        log_exception(
            logger, e,
            action="document.upload.single",
            message="上传文件失败，已回滚并退回配额",
            filename=getattr(file, "filename", ""),
        )
        update_upload_progress(task_upload_id, status="failed", message="上传失败，请稍后重试")
        await notify_task_failed(task_upload_id, "文件处理失败")
        await db.rollback()
        if quota_consumed:
            await refund_upload_quota(current_user.user_id, actual_size)
        raise HTTPException(status_code=500, detail="上传文件失败，请稍后重试")


@router.post("/batch")
async def batch_upload(
    files: List[UploadFile] = File(...),
    kb_id: Optional[str] = Query(None, description="知识库ID，不传则使用默认知识库"),
    chunk_size: Optional[int] = Query(None, ge=50, le=2000, description="文本块大小（50-2000）"),
    chunk_overlap: Optional[int] = Query(None, ge=0, le=500, description="文本块重叠大小（0-500）"),
    background_tasks: BackgroundTasks = None,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    kb = await _resolve_target_kb(db, kb_id, current_user)
    kb_id = str(kb.id)

    # SEC-2：批量上传的请求级上限（文件数 / 总字节）；单文件仍受 MAX_UPLOAD_SIZE_MB
    max_files = settings.security.UPLOAD_MAX_FILES_PER_BATCH
    if len(files) > max_files:
        raise HTTPException(
            status_code=413,
            detail=f"单次最多上传 {max_files} 个文件",
        )
    batch_limit = settings.security.UPLOAD_MAX_BATCH_MB * 1024 * 1024
    batch_total = 0

    from src.services.minio_service import MinioService

    results = []
    minio_service = await MinioService.get_instance()

    # 结束 kb 校验遗留的隐式事务并释放连接：上传循环可达数十秒，
    # 不得让 session 带着打开的事务/连接横跨 MinIO I/O（close 后复用 session 会自动重新 begin）
    await db.close()

    for file in files:
        _, ext = os.path.splitext(file.filename)
        if ext.lower() not in SUPPORTED_EXTENSIONS:
            results.append({"filename": file.filename, "status": "failed", "reason": "不支持的文件类型"})
            continue

        try:
            quota_reserved = 0
            # 大小上限校验（实测字节数）；超限计入该文件的失败原因
            actual_size = _ensure_upload_size(file)

            # SEC-2：单批总量 + 用户日配额
            if batch_total + actual_size > batch_limit:
                results.append({
                    "filename": file.filename,
                    "status": "failed",
                    "reason": f"超过单批总量上限 {settings.security.UPLOAD_MAX_BATCH_MB} MB",
                })
                continue
            if not await consume_upload_quota(current_user.user_id, actual_size):
                results.append({
                    "filename": file.filename,
                    "status": "failed",
                    "reason": "上传日配额已用尽",
                })
                continue
            quota_reserved = actual_size
            batch_total += actual_size

            doc_id = uuid.uuid4()
            file_path = await minio_service.upload_file_async(file, doc_id)

            # 创建文档记录，标记为处理中
            doc = Document(
                id=doc_id,
                filename=file.filename,
                file_path=file_path,
                file_type=ext.lower(),
                size=actual_size,
                kb_id=uuid.UUID(kb_id) if kb_id else None,
                owner_id=current_user.user_id,
                status="draft",
                processing_status="processing",
                processing_message="正在处理文档...",
                processing_progress=5,
                chunks_count=0,
                tags=[]
            )
            db.add(doc)
            await db.commit()

            task_upload_id = f"batch_{doc_id}"
            create_upload_progress(task_upload_id, file.filename, actual_size, owner_id=current_user.user_id)
            update_upload_progress(task_upload_id, status="processing", message="正在处理文档...")

            # 将文档处理添加到后台任务
            background_tasks.add_task(
                process_document_async,
                str(doc_id),
                file_path,
                str(kb_id) if kb_id else "",
                task_upload_id,
                chunk_size,
                chunk_overlap
            )

            results.append({
                "filename": file.filename,
                "status": "processing",
                "doc_id": str(doc_id),
                "upload_id": task_upload_id
            })
        except HTTPException as e:
            # 校验类失败（413 超限等）：保留业务提示，不泄露内部错误
            if quota_reserved:
                await refund_upload_quota(current_user.user_id, quota_reserved)
                batch_total -= quota_reserved
            results.append({"filename": file.filename, "status": "failed", "reason": str(e.detail)})
        except Exception as e:
            log_exception(
                logger, e,
                action="document.upload.batch_item",
                message="批量上传单文件失败，已退回该文件配额",
                filename=file.filename,
            )
            if quota_reserved:
                await refund_upload_quota(current_user.user_id, quota_reserved)
                batch_total -= quota_reserved
            results.append({"filename": file.filename, "status": "failed", "reason": "处理失败，请稍后重试"})

    # 使知识库列表缓存失效，确保批量上传后文档数量实时更新
    try:
        await invalidate_kb_list_cache(current_user.user_id)
    except Exception as exc:
        log_exception(
            logger, exc,
            action="document.upload.invalidate_kb_list_cache_batch",
            severity=ErrorSeverity.RECOVERABLE,
            message="批量上传后刷新知识库列表缓存失败，计数可能短暂滞后",
        )

    return {"results": results}


@router.get("/chunk-config")
async def get_chunk_config():
    return {
        "chunk_size": settings.processing.CHUNK_SIZE,
        "chunk_overlap": settings.processing.CHUNK_OVERLAP,
        "supported_range": {
            "chunk_size": {"min": 50, "max": 2000, "default": settings.processing.CHUNK_SIZE},
            "chunk_overlap": {"min": 0, "max": 500, "default": settings.processing.CHUNK_OVERLAP}
        },
        "recommendation": {
            "small_documents": {"chunk_size": 300, "chunk_overlap": 30},
            "medium_documents": {"chunk_size": 500, "chunk_overlap": 50},
            "large_documents": {"chunk_size": 800, "chunk_overlap": 80}
        }
    }


@router.get("/upload/progress/{upload_id}")
def get_upload_progress_endpoint(
    upload_id: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    """获取上传进度（轮询方式）"""
    progress = get_upload_progress(upload_id)
    if not progress:
        raise HTTPException(status_code=404, detail="上传任务不存在")
    # 归属校验：非本人的进度任务视同不存在，避免探测他人任务
    if progress.owner_id and progress.owner_id != current_user.user_id:
        raise HTTPException(status_code=404, detail="上传任务不存在")
    return progress.to_dict()


@ws_router.websocket("/upload/progress/ws/{upload_id}")
async def upload_progress_ws(websocket: WebSocket, upload_id: str):
    """WebSocket 实时推送上传进度

    认证方式：连接后首帧发送 {"type": "auth", "api_key": "..."} 完成鉴权（首帧鉴权，
    避免密钥进入反向代理访问日志）。认证失败或超时以 1008 关闭连接。
    """
    # accept 与首帧鉴权均在 get_current_user_for_ws 内完成
    current_user = await get_current_user_for_ws(websocket)

    # 归属校验：非本人的进度任务拒绝订阅
    progress = get_upload_progress(upload_id)
    if progress and progress.owner_id and progress.owner_id != current_user.user_id:
        await websocket.close(code=1008, reason="无权访问该上传任务")
        return

    # 注册 WebSocket 连接
    register_ws_connection(upload_id, websocket)
    
    try:
        # 首先发送当前进度（如果存在）
        progress = get_upload_progress(upload_id)
        if progress:
            await websocket.send_json(progress.to_dict())
        
        # 保持连接
        while True:
            # 定期发送心跳或等待消息
            data = await websocket.receive_json()
            if data.get("action") == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        # 连接断开，注销
        unregister_ws_connection(upload_id, websocket)
    except Exception as e:
        # WS 连接异常通常是客户端行为（提前断开/协议错误），保留上下文便于区分
        log_exception(
            logger, e,
            action="document.upload.ws_progress",
            severity=ErrorSeverity.RECOVERABLE,
            message="上传进度 WS 连接异常，已注销连接",
            upload_id=upload_id,
        )
        unregister_ws_connection(upload_id, websocket)
