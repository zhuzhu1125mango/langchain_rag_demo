"""
WebSocket通知路由 - 提供实时通知通道

提供WebSocket端点用于：
1. 订阅知识库列表变更通知
2. 订阅文档列表变更通知
3. 订阅特定任务/上传的进度通知
"""

import asyncio
import json
import uuid
from typing import List, Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, Query, Path, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.websockets import WebSocketState

from src.auth import get_current_user_for_ws, require_owner, CurrentUser
from src.database import get_db
from src.exceptions import BadRequestException, ResourceNotFoundException
from src.models.knowledge_base import KnowledgeBase
from src.services.notification_service import (
    subscribe,
    unsubscribe,
    unsubscribe_all,
    NotificationType,
    Notification
)
from src.services.progress_manager import progress_store

router = APIRouter(tags=["notifications"])


def _is_internal_user(current_user: CurrentUser) -> bool:
    """全局频道（kb:* / doc:*）仅内部/管理员：API Key 用户与开发默认用户。"""
    return current_user.user_id in {"api_key_user", "default"}


def _is_owned_doc_channel(channel: str) -> Optional[str]:
    """若为按知识库归属的频道（doc:{kb_id}），返回 kb_id；否则返回 None。"""
    prefix = "doc:"
    if channel.startswith(prefix):
        kb_id = channel[len(prefix):]
        if kb_id and kb_id != "*":
            return kb_id
    return None


def _task_owned(task_id: str, current_user: CurrentUser) -> bool:
    progress = progress_store.get(task_id)
    if progress is None:
        return False
    owner_id = getattr(progress, "owner_id", None)
    return bool(owner_id) and owner_id == current_user.user_id


async def _assert_channel_allowed(
    db: AsyncSession, channel: str, current_user: CurrentUser
) -> None:
    """SEC-8：全局频道仅内部；doc/task 频道校验资源归属；其余频道一律拒绝。"""
    if channel.startswith("user:"):
        if channel[len("user:"):] != current_user.user_id and not _is_internal_user(current_user):
            raise BadRequestException("无权订阅该用户频道")
        return
    if channel in {"kb:*", "doc:*"}:
        if not _is_internal_user(current_user):
            raise BadRequestException("全局频道仅管理员/内部服务可订阅")
        return
    if channel.startswith("doc:"):
        await _assert_doc_channel_owned(db, channel, current_user)
        return
    if channel.startswith("kb:"):
        kb_id = channel[len("kb:"):]
        if not kb_id or kb_id == "*":
            if not _is_internal_user(current_user):
                raise BadRequestException("全局频道仅管理员/内部服务可订阅")
            return
        await _assert_doc_channel_owned(db, f"doc:{kb_id}", current_user)
        return
    if channel.startswith("task:"):
        task_id = channel[len("task:"):]
        if not _task_owned(task_id, current_user) and not _is_internal_user(current_user):
            raise BadRequestException("无权订阅该任务频道")
        return
    raise BadRequestException("不支持的通知频道")


async def _assert_doc_channel_owned(
    db: AsyncSession, channel: str, current_user: CurrentUser
) -> None:
    """校验 doc:{kb_id} 频道对应知识库属于当前用户，否则抛 AuthorizationException(403)。"""
    kb_id = _is_owned_doc_channel(channel)
    if kb_id is None:
        return
    try:
        kb_uuid = uuid.UUID(kb_id)
    except ValueError:
        raise BadRequestException("无效的知识库ID")
    kb = (
        await db.execute(select(KnowledgeBase).filter(KnowledgeBase.id == kb_uuid))
    ).scalar_one_or_none()
    if not kb:
        raise ResourceNotFoundException("知识库不存在")
    require_owner(kb.owner_id, current_user)


class ConnectionManager:
    """WebSocket连接管理器"""

    def __init__(self):
        # connection_id -> (websocket, channels)
        self.active_connections: dict = {}

    async def connect(
        self,
        websocket: WebSocket,
        connection_id: str,
        channels: List[str],
        user_id: Optional[str] = None,
    ):
        """建立连接并订阅频道"""
        # 首帧鉴权流程已在 get_current_user_for_ws 中 accept，此处幂等处理
        if websocket.client_state != WebSocketState.CONNECTED:
            await websocket.accept()
        self.active_connections[connection_id] = {
            "websocket": websocket,
            "channels": set(channels),
            "user_id": user_id,
        }

        # 订阅到指定频道
        for channel in channels:
            subscribe(channel, websocket)

        # 发送连接成功消息
        await websocket.send_json({
            "type": "connected",
            "connection_id": connection_id,
            "channels": channels
        })

    def disconnect(self, connection_id: str):
        """断开连接"""
        if connection_id in self.active_connections:
            channels = self.active_connections[connection_id]["channels"]
            websocket = self.active_connections[connection_id]["websocket"]
            for channel in channels:
                unsubscribe(channel, websocket)
            del self.active_connections[connection_id]

    async def send_personal_message(self, connection_id: str, message: dict):
        """发送个人消息"""
        if connection_id in self.active_connections:
            websocket = self.active_connections[connection_id]["websocket"]
            try:
                await websocket.send_json(message)
            except Exception:
                self.disconnect(connection_id)

    async def broadcast(self, channel: str, message: dict):
        """广播消息到频道（由notification_service调用）"""
        # 这个方法由notification_service使用
        pass


manager = ConnectionManager()


@router.websocket("/ws/notifications")
async def websocket_notifications(
    websocket: WebSocket,
    channels: str = Query(default="", description="逗号分隔的订阅频道；为空时仅订阅当前用户频道"),
    db: AsyncSession = Depends(get_db),
):
    """
    通用通知WebSocket端点

    订阅频道示例：
    - kb:* - 订阅所有知识库相关通知
    - doc:{kb_id} - 订阅特定知识库的文档通知
    - task:{task_id} - 订阅特定任务的进度通知

    消息格式：
    {
        "type": "connected" | "kb_list_changed" | "doc_list_changed" | "task_progress" | "task_completed" | "task_failed",
        "data": {...}
    }

    认证方式：连接后首帧发送 {"type": "auth", "api_key": "..."} 完成鉴权（首帧鉴权）。
    """
    current_user = await get_current_user_for_ws(websocket)
    connection_id = str(uuid.uuid4())

    # 解析频道列表；SEC-8 默认只订阅当前用户频道（全局频道仅内部）
    channel_list = [c.strip() for c in channels.split(",") if c.strip()]
    if not channel_list:
        channel_list = [f"user:{current_user.user_id}"]

    # 越权校验：user/doc/kb/task 频道均需通过归属校验
    for channel in channel_list:
        try:
            await _assert_channel_allowed(db, channel, current_user)
        except (HTTPException, BadRequestException):
            await websocket.close(code=1008, reason="无权订阅该频道")
            return

    await manager.connect(websocket, connection_id, channel_list, user_id=current_user.user_id)

    try:
        while True:
            # 保持连接，接收心跳
            data = await websocket.receive_text()

            try:
                message = json.loads(data)

                # 处理心跳
                if message.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})

                # 处理频道订阅变更
                elif message.get("type") == "subscribe":
                    new_channels = message.get("channels", [])
                    valid_new = []
                    for channel in new_channels:
                        try:
                            await _assert_channel_allowed(db, channel, current_user)
                        except (HTTPException, BadRequestException):
                            # 越权频道：拒绝订阅，但不中断连接
                            continue
                        subscribe(channel, websocket)
                        manager.active_connections[connection_id]["channels"].add(channel)
                        valid_new.append(channel)
                    await websocket.send_json({
                        "type": "subscribed",
                        "channels": valid_new
                    })

                elif message.get("type") == "unsubscribe":
                    old_channels = message.get("channels", [])
                    for channel in old_channels:
                        unsubscribe(channel, websocket)
                        manager.active_connections[connection_id]["channels"].discard(channel)
                    await websocket.send_json({
                        "type": "unsubscribed",
                        "channels": list(old_channels)
                    })

            except json.JSONDecodeError:
                # 忽略无效JSON
                pass

    except WebSocketDisconnect:
        manager.disconnect(connection_id)
    except Exception:
        manager.disconnect(connection_id)


@router.websocket("/ws/kb")
async def websocket_knowledge_bases(websocket: WebSocket):
    """
    知识库列表变更通知WebSocket

    订阅此端点后，会收到知识库列表变更通知：
    - kb_list_changed: 知识库列表已变更
    - kb_created: 新建知识库
    - kb_updated: 知识库已更新
    - kb_deleted: 知识库已删除

    认证方式：连接后首帧发送 {"type": "auth", "api_key": "..."} 完成鉴权（首帧鉴权）。
    """
    current_user = await get_current_user_for_ws(websocket)
    connection_id = str(uuid.uuid4())

    await manager.connect(websocket, connection_id, [f"user:{current_user.user_id}"], user_id=current_user.user_id)

    try:
        while True:
            data = await websocket.receive_text()

            # 心跳处理
            if data == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        manager.disconnect(connection_id)
    except Exception:
        manager.disconnect(connection_id)


@router.websocket("/ws/docs/{kb_id}")
async def websocket_documents(
    websocket: WebSocket,
    kb_id: str = Path(..., description="知识库ID"),
    db: AsyncSession = Depends(get_db),
):
    """
    文档列表变更通知WebSocket

    订阅特定知识库的文档变更通知：
    - doc_list_changed: 文档列表已变更
    - doc_created: 新文档上传完成
    - doc_deleted: 文档已删除
    - doc_processing: 文档处理进度更新

    认证方式：连接后首帧发送 {"type": "auth", "api_key": "..."} 完成鉴权（首帧鉴权）。
    权限校验：仅允许知识库所有者或全局 API Key 用户订阅。
    """
    current_user = await get_current_user_for_ws(websocket)

    # 校验知识库存在性与所有权
    try:
        kb_id_uuid = uuid.UUID(kb_id)
    except ValueError:
        await websocket.close(code=1008, reason="无效的知识库ID")
        return

    result = await db.execute(
        select(KnowledgeBase).filter(KnowledgeBase.id == kb_id_uuid)
    )
    kb = result.scalar_one_or_none()
    if not kb:
        await websocket.close(code=1008, reason="知识库不存在")
        return

    try:
        require_owner(kb.owner_id, current_user)
    except Exception:
        await websocket.close(code=1008, reason="无权访问该知识库")
        return

    connection_id = str(uuid.uuid4())
    channels = [f"doc:{kb_id}", "doc:*"]  # 订阅特定KB和全局
    await manager.connect(websocket, connection_id, channels, user_id=current_user.user_id)

    try:
        while True:
            data = await websocket.receive_text()

            # 心跳处理
            if data == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        manager.disconnect(connection_id)
    except Exception:
        manager.disconnect(connection_id)

