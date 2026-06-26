"""MinIO 对象存储服务。

负责文档原始文件的存储、下载、删除和临时访问链接生成。
使用单例模式管理 MinIO 客户端连接，并在首次访问时自动创建 bucket。
"""

import asyncio
import logging
import threading
import uuid
from minio import Minio
from minio.error import S3Error

from src.config import settings

logger = logging.getLogger("minio_service")


class MinioService:
    """MinIO 客户端封装，提供同步与异步文件操作。"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        """创建或返回单例，首次创建时初始化客户端并确保 bucket 存在。"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._client = cls._create_client()
                    cls._instance._ensure_bucket()
        return cls._instance

    @staticmethod
    def _create_client():
        """根据配置创建 MinIO 客户端。"""
        return Minio(
            settings.minio.MINIO_ENDPOINT,
            access_key=settings.minio.MINIO_ACCESS_KEY,
            secret_key=settings.minio.MINIO_SECRET_KEY,
            secure=settings.minio.MINIO_SECURE
        )

    def _ensure_bucket(self):
        """确保配置的 bucket 已存在，不存在则自动创建。"""
        try:
            if not self._client.bucket_exists(settings.minio.MINIO_BUCKET_NAME):
                self._client.make_bucket(settings.minio.MINIO_BUCKET_NAME)
                logger.info(f"Created bucket: {settings.minio.MINIO_BUCKET_NAME}")
        except S3Error as e:
            logger.error(f"Error creating bucket: {e}")

    def upload_file(self, file, file_id=None):
        """同步上传文件到 MinIO。

        Args:
            file: FastAPI UploadFile 对象或类似文件对象。
            file_id: 可选文件 ID，默认自动生成 UUID。

        Returns:
            str: minio:// 协议的文件访问地址。
        """
        file_key = f"documents/{file_id or uuid.uuid4()}/{file.filename}"

        file.file.seek(0)

        self._client.put_object(
            settings.minio.MINIO_BUCKET_NAME,
            file_key,
            file.file,
            file.size,
            content_type=file.content_type
        )

        return f"minio://{settings.minio.MINIO_BUCKET_NAME}/{file_key}"

    def download_file(self, file_key, local_path):
        """同步下载文件到本地路径。"""
        actual_key = file_key.replace(f"minio://{settings.minio.MINIO_BUCKET_NAME}/", "")
        self._client.fget_object(settings.minio.MINIO_BUCKET_NAME, actual_key, local_path)

    def delete_file(self, file_key):
        """同步删除 MinIO 中的文件。"""
        actual_key = file_key.replace(f"minio://{settings.minio.MINIO_BUCKET_NAME}/", "")
        self._client.remove_object(settings.minio.MINIO_BUCKET_NAME, actual_key)

    def get_file_url(self, file_key, expires=3600):
        """同步生成带签名的临时访问 URL。

        Args:
            file_key: minio:// 协议的文件地址。
            expires: 链接有效期（秒），默认 1 小时。
        """
        actual_key = file_key.replace(f"minio://{settings.minio.MINIO_BUCKET_NAME}/", "")
        return self._client.presigned_get_object(settings.minio.MINIO_BUCKET_NAME, actual_key, expires=expires)

    def file_exists(self, file_key):
        """同步检查文件是否存在。"""
        try:
            actual_key = file_key.replace(f"minio://{settings.minio.MINIO_BUCKET_NAME}/", "")
            self._client.stat_object(settings.minio.MINIO_BUCKET_NAME, actual_key)
            return True
        except S3Error as e:
            if e.code == "NoSuchKey":
                return False
            raise

    async def upload_file_async(self, file, file_id=None):
        """异步上传文件（在线程池中执行同步操作）。"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self.upload_file,
            file,
            file_id
        )

    async def download_file_async(self, file_key, local_path):
        """异步下载文件。"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self.download_file,
            file_key,
            local_path
        )

    async def delete_file_async(self, file_key):
        """异步删除文件。"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self.delete_file,
            file_key
        )

    async def get_file_url_async(self, file_key, expires=3600):
        """异步生成临时访问 URL。"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self.get_file_url,
            file_key,
            expires
        )

    async def file_exists_async(self, file_key):
        """异步检查文件是否存在。"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self.file_exists,
            file_key
        )