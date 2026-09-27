"""W4 硬化批次单元测试。

覆盖：CacheService.clear_pattern 的 SCAN 游标改造（W4-20）、
运行时配置覆盖落盘/加载（W4-23）、MinIO 对象键去除用户可控文件名（W4-25）。
"""

import io
import os
import uuid

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from src.config import settings
from src.services.cache_service import CacheService
from src.services.minio_service import MinioService
from src.services import runtime_config_service


# ---------------------------------------------------------------------------
# W4-20: clear_pattern 用 SCAN 游标增量遍历
# ---------------------------------------------------------------------------


class _FakeScanRedis:
    """scan 游标按页返回，delete 记录调用。"""

    def __init__(self, pages):
        self._pages = pages  # {cursor: (next_cursor, [keys])}
        self.scan_calls = []
        self.deleted_batches = []

    async def scan(self, cursor=0, match=None, count=100):
        self.scan_calls.append({"cursor": cursor, "match": match, "count": count})
        return self._pages[cursor]

    async def delete(self, *keys):
        self.deleted_batches.append(list(keys))
        return len(keys)


def _bare_cache(fake_redis) -> CacheService:
    cache = CacheService.__new__(CacheService)
    cache._available = True
    cache._unavailable_until = 0.0
    cache.client = fake_redis
    return cache


@pytest.mark.asyncio
async def test_clear_pattern_uses_scan_and_deletes_in_batches():
    pages = {0: (17, [b"k1", b"k2"]), 17: (0, [b"k3"])}
    redis = _FakeScanRedis(pages)
    cache = _bare_cache(redis)

    await cache.clear_pattern("kb:*")

    # scan 循环到游标归零；每页立即删除，而非 KEYS 一次性全量
    assert [c["cursor"] for c in redis.scan_calls] == [0, 17]
    assert all(c["count"] == 100 for c in redis.scan_calls)
    assert redis.deleted_batches == [[b"k1", b"k2"], [b"k3"]]


@pytest.mark.asyncio
async def test_clear_pattern_no_match_is_noop():
    redis = _FakeScanRedis({0: (0, [])})
    cache = _bare_cache(redis)

    await cache.clear_pattern("nothing:*")

    assert redis.deleted_batches == []


# ---------------------------------------------------------------------------
# W4-23: 运行时配置覆盖落盘与加载
# ---------------------------------------------------------------------------


@pytest.fixture
def _tmp_overrides_path(monkeypatch, tmp_path):
    """把覆盖文件指到临时目录，测试后恢复内存配置。"""
    saved = {
        "CHUNK_SIZE": settings.processing.CHUNK_SIZE,
        "CHUNK_OVERLAP": settings.processing.CHUNK_OVERLAP,
        "TOP_K": settings.processing.TOP_K,
    }
    path = tmp_path / "runtime_config.json"
    monkeypatch.setattr(runtime_config_service, "_OVERRIDES_PATH", str(path))
    yield path
    settings.processing.CHUNK_SIZE = saved["CHUNK_SIZE"]
    settings.processing.CHUNK_OVERLAP = saved["CHUNK_OVERLAP"]
    settings.processing.TOP_K = saved["TOP_K"]


class TestRuntimeConfigOverrides:
    """save/load 往返、白名单约束与损坏文件容错。"""

    def test_save_then_load_roundtrip(self, _tmp_overrides_path):
        settings.processing.CHUNK_SIZE = 777
        settings.processing.TOP_K = 9
        runtime_config_service.save_runtime_overrides()
        assert _tmp_overrides_path.exists()

        # 模拟重启：内存回到基线后加载覆盖
        settings.processing.CHUNK_SIZE = 512
        settings.processing.TOP_K = 3
        runtime_config_service.load_runtime_overrides()

        assert settings.processing.CHUNK_SIZE == 777
        assert settings.processing.TOP_K == 9

    def test_load_ignores_non_whitelisted_keys(self, _tmp_overrides_path):
        _tmp_overrides_path.write_text(
            '{"processing": {"CHUNK_SIZE": 700, "SECRET_KEY": "hack"}, "model": {"OLLAMA_MODEL_NAME": "evil"}}',
            encoding="utf-8",
        )
        runtime_config_service.load_runtime_overrides()

        assert settings.processing.CHUNK_SIZE == 700  # 白名单键生效
        assert settings.model.OLLAMA_MODEL_NAME != "evil"  # 非 processing 节整体被忽略

    def test_load_tolerates_corrupted_file(self, _tmp_overrides_path):
        _tmp_overrides_path.write_text("{not-json", encoding="utf-8")
        runtime_config_service.load_runtime_overrides()  # 不抛异常
        assert settings.processing.CHUNK_SIZE == 512  # 保持基线

    def test_load_missing_file_is_noop(self, _tmp_overrides_path):
        runtime_config_service.load_runtime_overrides()  # 文件不存在，静默返回


# ---------------------------------------------------------------------------
# W4-25: MinIO 对象键不含用户可控原始文件名
# ---------------------------------------------------------------------------


class _FakeMinioClient:
    """捕获 put_object 的对象键与 content_type。"""

    def __init__(self):
        self.uploaded = []

    def bucket_exists(self, bucket):
        return True

    def put_object(self, bucket, key, data, length, content_type=None):
        self.uploaded.append({"bucket": bucket, "key": key, "length": length, "content_type": content_type})


def _bare_minio_service(fake_client) -> MinioService:
    service = MinioService.__new__(MinioService)
    service._client = fake_client
    return service


def _upload_file(filename: str, content: bytes = b"data") -> UploadFile:
    headers = Headers({"content-type": "application/octet-stream"})
    f = UploadFile(filename=filename, file=io.BytesIO(content), headers=headers)
    f.size = len(content)
    return f


class TestMinioObjectKey:
    """新上传对象键格式 documents/{id}/source{ext}。"""

    def test_key_uses_extension_only(self):
        client = _FakeMinioClient()
        service = _bare_minio_service(client)

        fixed_id = str(uuid.uuid4())
        service.upload_file(_upload_file("我的 报告 v2.pdf"), file_id=fixed_id)

        assert client.uploaded[0]["key"] == f"documents/{fixed_id}/source.pdf"

    def test_filename_never_appears_in_key(self):
        client = _FakeMinioClient()
        service = _bare_minio_service(client)

        fixed_id = str(uuid.uuid4())
        service.upload_file(_upload_file("../../weird name<>.txt"), file_id=fixed_id)

        key = client.uploaded[0]["key"]
        assert key == f"documents/{fixed_id}/source.txt"
        assert "weird" not in key and ".." not in key

    def test_missing_extension_falls_back_to_bin(self):
        client = _FakeMinioClient()
        service = _bare_minio_service(client)

        fixed_id = str(uuid.uuid4())
        service.upload_file(_upload_file("noext"), file_id=fixed_id)

        assert client.uploaded[0]["key"] == f"documents/{fixed_id}/source.bin"

    def test_returned_uri_roundtrips_with_delete(self):
        client = _FakeMinioClient()
        service = _bare_minio_service(client)

        uri = service.upload_file(_upload_file("doc.docx"), file_id=str(uuid.uuid4()))
        assert uri.startswith(f"minio://{settings.minio.MINIO_BUCKET_NAME}/")
