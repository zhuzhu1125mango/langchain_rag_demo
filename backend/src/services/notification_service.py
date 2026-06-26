"""
实时通知服务 - WebSocket广播通知

用于推送知识库和文档的实时变更通知：
1. 知识库列表变更（创建/更新/删除）
2. 文档列表变更（上传/删除）
3. 任务状态变更（异步任务进度）
"""

import asyncio
import json
from typing import Dict, Set, Optional, Any
from dataclasses import dataclass, asdict
from enum import Enum
from datetime import datetime

# WebSocket连接管理
# channel -> set of WebSocket connections
channel_connections: Dict[str, Set] = {}


class NotificationType(str, Enum):
    """通知类型枚举"""
    # 知识库相关
    KB_LIST_CHANGED = "kb_list_changed"
    KB_CREATED = "kb_created"
    KB_UPDATED = "kb_updated"
    KB_DELETED = "kb_deleted"

    # 文档相关
    DOC_LIST_CHANGED = "doc_list_changed"
    DOC_CREATED = "doc_created"
    DOC_UPDATED = "doc_updated"
    DOC_DELETED = "doc_deleted"
    DOC_PROCESSING = "doc_processing"

    # 任务相关
    TASK_PROGRESS = "task_progress"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"


@dataclass
class Notification:
    """通知数据类"""
    type: NotificationType
    channel: str
    data: Dict[str, Any]
    timestamp: str = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now().isoformat()

    def to_dict(self) -> dict:
        return asdict(self)


def get_channel(channel: str) -> Set:
    """获取频道的所有连接"""
    if channel not in channel_connections:
        channel_connections[channel] = set()
    return channel_connections[channel]


def subscribe(channel: str, websocket) -> None:
    """订阅频道"""
    connections = get_channel(channel)
    connections.add(websocket)


def unsubscribe(channel: str, websocket) -> None:
    """取消订阅"""
    if channel in channel_connections:
        channel_connections[channel].discard(websocket)


def unsubscribe_all(websocket) -> None:
    """取消订阅所有频道"""
    for channel in channel_connections:
        channel_connections[channel].discard(websocket)


async def broadcast(channel: str, notification: Notification) -> None:
    """
    向指定频道的所有连接广播通知

    Args:
        channel: 频道名称 (如 "kb:*" 表示所有知识库相关通知)
        notification: 通知数据
    """
    connections = get_channel(channel).copy()
    if not connections:
        return

    message = notification.to_dict()

    # 并发发送给所有连接
    dead_connections = set()
    for ws in connections:
        try:
            await ws.send_json(message)
        except Exception:
            # 标记无效连接
            dead_connections.add(ws)

    # 从原集合中移除无效连接
    if dead_connections:
        channel_connections[channel] = connections - dead_connections


async def broadcast_all(notification: Notification) -> None:
    """
    向所有连接的客户端广播通知
    """
    for channel in list(channel_connections.keys()):
        await broadcast(channel, notification)


async def notify_kb_list_changed(kb_id: str = None, action: str = "updated") -> None:
    """
    通知知识库列表已变更

    Args:
        kb_id: 变更的知识库ID
        action: 操作类型 (created/updated/deleted)
    """
    notification = Notification(
        type=NotificationType.KB_LIST_CHANGED,
        channel="kb:*",
        data={
            "action": action,
            "kb_id": kb_id
        }
    )
    await broadcast("kb:*", notification)


async def notify_doc_list_changed(kb_id: str, doc_id: str = None, action: str = "updated") -> None:
    """
    通知文档列表已变更

    Args:
        kb_id: 所属知识库ID
        doc_id: 变更的文档ID
        action: 操作类型 (created/deleted/updated)
    """
    # 通知全局文档列表变更
    global_notification = Notification(
        type=NotificationType.DOC_LIST_CHANGED,
        channel="doc:*",
        data={
            "action": action,
            "kb_id": kb_id,
            "doc_id": doc_id
        }
    )
    await broadcast("doc:*", global_notification)

    # 通知特定知识库的文档列表变更
    kb_channel = f"doc:{kb_id}"
    kb_notification = Notification(
        type=NotificationType.DOC_LIST_CHANGED,
        channel=kb_channel,
        data={
            "action": action,
            "kb_id": kb_id,
            "doc_id": doc_id
        }
    )
    await broadcast(kb_channel, kb_notification)


async def notify_task_progress(task_id: str, progress: int, message: str = "", data: Dict = None) -> None:
    """
    通知任务进度

    Args:
        task_id: 任务ID
        progress: 进度百分比 (0-100)
        message: 进度消息
        data: 额外数据
    """
    notification = Notification(
        type=NotificationType.TASK_PROGRESS,
        channel=f"task:{task_id}",
        data={
            "task_id": task_id,
            "progress": progress,
            "message": message,
            **(data or {})
        }
    )
    await broadcast(f"task:{task_id}", notification)


async def notify_task_completed(task_id: str, data: Dict = None) -> None:
    """
    通知任务完成
    """
    notification = Notification(
        type=NotificationType.TASK_COMPLETED,
        channel=f"task:{task_id}",
        data={
            "task_id": task_id,
            **(data or {})
        }
    )
    await broadcast(f"task:{task_id}", notification)


async def notify_task_failed(task_id: str, error: str) -> None:
    """
    通知任务失败
    """
    notification = Notification(
        type=NotificationType.TASK_FAILED,
        channel=f"task:{task_id}",
        data={
            "task_id": task_id,
            "error": error
        }
    )
    await broadcast(f"task:{task_id}", notification)


# 频道通配符匹配
async def broadcast_matched(channel_pattern: str, notification: Notification) -> None:
    """
    向匹配频道模式的所有连接广播

    Args:
        channel_pattern: 频道模式 (支持 * 通配符)
        notification: 通知数据
    """
    # 查找匹配的频道
    for channel in list(channel_connections.keys()):
        if match_channel(channel, channel_pattern):
            await broadcast(channel, notification)


def match_channel(channel: str, pattern: str) -> bool:
    """检查频道是否匹配模式"""
    if pattern == "*":
        return True
    if "*" in pattern:
        # 简单通配符匹配
        import re
        regex = pattern.replace("*", ".*")
        return re.match(f"^{regex}$", channel) is not None
    return channel == pattern


# 便捷函数：创建并发送通知
async def send_notification(
    notification_type: NotificationType,
    channel: str,
    data: Dict[str, Any]
) -> None:
    """发送通知的便捷方法"""
    notification = Notification(
        type=notification_type,
        channel=channel,
        data=data
    )
    await broadcast(channel, notification)
