"""
进度管理器 - 用于跟踪文档上传和处理进度

本模块负责：
1. 管理上传任务的进度状态
2. 支持 WebSocket 实时推送进度
3. 提供进度查询接口
"""

import asyncio
from typing import Dict, Optional, Any
from uuid import UUID

# 全局进度存储
progress_store: Dict[str, dict] = {}
# WebSocket 连接存储（上传ID -> 连接列表）
ws_connections: Dict[str, list] = {}


class UploadProgress:
    """上传进度类"""

    def __init__(self, upload_id: str, file_name: str, file_size: int):
        self.upload_id = upload_id
        self.file_name = file_name
        self.file_size = file_size
        self.uploaded_bytes = 0
        self.status = "uploading"  # uploading, processing, completed, failed
        self.message = "上传中..."
        self.processing_progress = 0
        self.total_files = 1
        self.current_file = 1

    def to_dict(self):
        """转换为字典格式"""
        return {
            "upload_id": self.upload_id,
            "file_name": self.file_name,
            "file_size": self.file_size,
            "uploaded_bytes": self.uploaded_bytes,
            "progress": self.calculate_progress(),
            "status": self.status,
            "message": self.message,
            "processing_progress": self.processing_progress,
            "total_files": self.total_files,
            "current_file": self.current_file
        }

    def calculate_progress(self) -> int:
        """计算总体进度百分比"""
        if self.status == "completed":
            return 100
        if self.status == "failed":
            return 0

        if self.file_size == 0:
            upload_progress = 0
        else:
            upload_progress = min(100, int((self.uploaded_bytes / self.file_size) * 100))

        # 上传占 30%，处理占 70%
        if self.status == "uploading":
            return upload_progress * 0.3
        else:  # processing
            return 30 + self.processing_progress * 0.7


def create_upload_progress(upload_id: str, file_name: str, file_size: int) -> UploadProgress:
    """创建上传进度记录"""
    progress = UploadProgress(upload_id, file_name, file_size)
    progress_store[upload_id] = progress
    return progress


def get_upload_progress(upload_id: str) -> Optional[UploadProgress]:
    """获取上传进度"""
    return progress_store.get(upload_id)


# 终态（completed/failed）记录保留窗口（秒）：留时间给 WS 客户端读取最终状态，
# 之后自动清理，避免进程内进度缓存只增不减（内存泄漏）。
TERMINAL_STATUSES = {"completed", "failed"}
PROGRESS_RETENTION_SECONDS = 60.0


def update_upload_progress(upload_id: str, **kwargs):
    """更新上传进度"""
    progress = progress_store.get(upload_id)
    if progress:
        for key, value in kwargs.items():
            if hasattr(progress, key):
                setattr(progress, key, value)
        # 通知所有 WebSocket 连接（使用线程池执行异步代码）
        try:
            loop = asyncio.get_running_loop()
            # 如果已经在事件循环中，调度任务
            loop.call_soon(lambda: asyncio.create_task(notify_ws_clients_async(upload_id)))
            # 到达终态后调度延迟清理
            if progress.status in TERMINAL_STATUSES:
                _schedule_terminal_cleanup(upload_id)
        except RuntimeError:
            # 如果没有事件循环，创建新事件循环
            pass


def _schedule_terminal_cleanup(upload_id: str):
    """终态记录延迟清理调度（保留 PROGRESS_RETENTION_SECONDS 秒）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.call_later(
        PROGRESS_RETENTION_SECONDS,
        lambda: asyncio.create_task(_cleanup_terminal_progress(upload_id)),
    )


async def _cleanup_terminal_progress(upload_id: str):
    """清理已达终态的进度记录与残留 WS 连接。"""
    progress = progress_store.get(upload_id)
    if progress is not None and progress.status in TERMINAL_STATUSES:
        remove_upload_progress(upload_id)
    connections = ws_connections.pop(upload_id, [])
    for conn in connections:
        try:
            await conn.close()
        except Exception:
            pass


async def notify_ws_clients_async(upload_id: str):
    """通知所有 WebSocket 客户端进度更新（异步版本）"""
    progress = progress_store.get(upload_id)
    if not progress:
        return

    connections = ws_connections.get(upload_id, [])
    if not connections:
        return

    message = progress.to_dict()

    # 异步发送给所有连接的客户端
    for conn in connections[:]:  # 使用切片复制列表避免迭代时修改
        try:
            await conn.send_json(message)
        except Exception:
            # 移除无效连接
            try:
                connections.remove(conn)
            except ValueError:
                pass

    # 更新连接列表
    ws_connections[upload_id] = connections


# 保持向后兼容的同步版本
def notify_ws_clients(upload_id: str):
    """通知所有 WebSocket 客户端进度更新（同步版本，仅用于非异步上下文）"""
    pass  # 由 update_upload_progress 中的异步调度处理


def remove_upload_progress(upload_id: str):
    """移除上传进度记录"""
    if upload_id in progress_store:
        del progress_store[upload_id]


def register_ws_connection(upload_id: str, connection):
    """注册 WebSocket 连接"""
    if upload_id not in ws_connections:
        ws_connections[upload_id] = []
    ws_connections[upload_id].append(connection)


def unregister_ws_connection(upload_id: str, connection):
    """注销 WebSocket 连接"""
    if upload_id in ws_connections:
        try:
            ws_connections[upload_id].remove(connection)
        except ValueError:
            pass
