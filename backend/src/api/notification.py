"""
WebSocket通知路由 - 提供实时通知通道

提供WebSocket端点用于：
1. 订阅知识库列表变更通知
2. 订阅文档列表变更通知
3. 订阅特定任务/上传的进度通知
"""

import asyncio
import json
from typing import List
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, Path
from src.services.notification_service import (
    subscribe,
    unsubscribe,
    unsubscribe_all,
    NotificationType,
    Notification
)

router = APIRouter(tags=["notifications"])


class ConnectionManager:
    """WebSocket连接管理器"""

    def __init__(self):
        # connection_id -> (websocket, channels)
        self.active_connections: dict = {}

    async def connect(self, websocket: WebSocket, connection_id: str, channels: List[str]):
        """建立连接并订阅频道"""
        await websocket.accept()
        self.active_connections[connection_id] = {
            "websocket": websocket,
            "channels": set(channels)
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
    channels: str = Query(default="kb:*,doc:*", description="逗号分隔的订阅频道，支持通配符如 kb:*")
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
    """
    import uuid
    connection_id = str(uuid.uuid4())

    # 解析频道列表
    channel_list = [c.strip() for c in channels.split(",") if c.strip()]

    await manager.connect(websocket, connection_id, channel_list)

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
                    for channel in new_channels:
                        subscribe(channel, websocket)
                        manager.active_connections[connection_id]["channels"].add(channel)
                    await websocket.send_json({
                        "type": "subscribed",
                        "channels": list(new_channels)
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
    """
    import uuid
    connection_id = str(uuid.uuid4())

    await manager.connect(websocket, connection_id, ["kb:*"])

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
    kb_id: str = Path(..., description="知识库ID")
):
    """
    文档列表变更通知WebSocket

    订阅特定知识库的文档变更通知：
    - doc_list_changed: 文档列表已变更
    - doc_created: 新文档上传完成
    - doc_deleted: 文档已删除
    - doc_processing: 文档处理进度更新
    """
    import uuid
    connection_id = str(uuid.uuid4())

    channels = [f"doc:{kb_id}", "doc:*"]  # 订阅特定KB和全局
    await manager.connect(websocket, connection_id, channels)

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

