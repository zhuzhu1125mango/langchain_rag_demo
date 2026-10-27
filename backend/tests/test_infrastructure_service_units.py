"""基础设施服务单元测试（SEC-15 覆盖率补测）。

覆盖 CacheService / MinioService / OllamaReranker / VectorStoreManager 的
正常、降级与异常分支；全部使用本地桩对象，不依赖真实 Redis/MinIO/Milvus。
"""

import io
import time
import types

import pytest
from minio.error import S3Error
from pymilvus.exceptions import MilvusException

from src.services.cache_service import CacheService
from src.services.minio_service import MinioService
from src.services.ollama_reranker import OllamaReranker
from src.services.vector_store import VectorStoreManager


# ---------------------------------------------------------------------------
# CacheService
# ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self, store=None, fail=False, scan_pages=None, ping_ok=True):
        self.store = dict(store or {})
        self.fail = fail
        self.ttl = None
        self.deleted = []
        self.scan_pages = scan_pages or []
        self.scan_calls = 0
        self.ping_ok = ping_ok
        self.ping_calls = 0

    def _check(self):
        if self.fail:
            raise RuntimeError("redis down")

    async def get(self, key):
        self._check()
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self._check()
        self.store[key] = value
        self.ttl = ex
        return True

    async def delete(self, *keys):
        self._check()
        for key in keys:
            self.store.pop(key, None)
        self.deleted.extend(keys)
        return len(keys)

    async def exists(self, key):
        self._check()
        return 1 if key in self.store else 0

    async def scan(self, cursor=0, match=None, count=100):
        self._check()
        page = self.scan_pages[self.scan_calls]
        self.scan_calls += 1
        return page

    async def ping(self):
        self.ping_calls += 1
        if not self.ping_ok:
            raise RuntimeError("ping failed")
        return True


def _cache_with(client):
    cache = CacheService()
    cache.client = client
    cache._available = True
    return cache


async def test_cache_get_serialization_paths():
    cache = _cache_with(_FakeRedis(store={"json": '{"a": 1}', "raw": "plain", "num": "12"}))
    assert await cache.get("json") == {"a": 1}
    assert await cache.get("raw") == "plain"
    assert await cache.get("num") == 12
    assert await cache.get("missing") is None


async def test_cache_get_failure_marks_unavailable():
    cache = _cache_with(_FakeRedis(fail=True))
    assert await cache.get("k") is None
    assert cache._available is False
    assert cache._unavailable_until > 0


async def test_cache_set_serializes_and_passes_expiry():
    from datetime import timedelta

    client = _FakeRedis()
    cache = _cache_with(client)
    assert await cache.set("d", {"a": 1}) is True
    assert client.store["d"] == '{"a": 1}'
    assert client.ttl is None

    assert await cache.set("l", [1, 2], expire=timedelta(seconds=30)) is True
    assert client.store["l"] == "[1, 2]"
    assert client.ttl == timedelta(seconds=30)

    assert await cache.set("raw", "text") is True
    assert client.store["raw"] == "text"

    failing = _cache_with(_FakeRedis(fail=True))
    assert await failing.set("k", "v") is False


async def test_cache_delete_and_exists():
    client = _FakeRedis(store={"a": "1"})
    cache = _cache_with(client)
    assert await cache.exists("a") is True
    assert await cache.exists("b") is False
    assert await cache.delete("a") == 1
    assert client.deleted == ["a"]

    failing = _cache_with(_FakeRedis(fail=True))
    assert await failing.delete("a") == 0
    assert await failing.exists("a") is False


async def test_cache_clear_pattern_scans_until_cursor_zero():
    pages = [(1, ["a", "b"]), (0, ["c"])]
    client = _FakeRedis(store={"a": "1", "b": "2", "c": "3"}, scan_pages=pages)
    cache = _cache_with(client)
    await cache.clear_pattern("rag:*")
    assert client.scan_calls == 2
    assert client.deleted == ["a", "b", "c"]

    failing = _cache_with(_FakeRedis(fail=True, scan_pages=pages))
    await failing.clear_pattern("rag:*")  # 失败静默，不抛出
    assert failing._available is False


async def test_cache_ping_and_recovery():
    client = _FakeRedis()
    cache = _cache_with(client)
    assert await cache.ping() is True

    cache._available = False
    cache._unavailable_until = 0.0
    assert await cache.ping() is True
    assert cache._available is True

    cache._available = False
    cache._unavailable_until = 0.0
    cache.client.ping_ok = False
    assert await cache.ping() is False
    assert cache._available is False
    assert cache._unavailable_until > time.monotonic()


async def test_cache_recovery_cooldown_skips_ping():
    client = _FakeRedis()
    cache = _cache_with(client)
    cache._available = False
    cache._unavailable_until = time.monotonic() + 100
    await cache._maybe_recover()
    assert client.ping_calls == 0
    assert cache._available is False

    # 无客户端时不做恢复探测
    cache.client = None
    await cache._maybe_recover()
    assert cache.available is False


async def test_execute_unavailable_closes_coroutine_and_raises():
    cache = _cache_with(_FakeRedis())
    cache._available = False
    cache._unavailable_until = time.monotonic() + 100
    coro = cache.client.get("k")
    with pytest.raises(ConnectionError, match="不可用"):
        await cache._execute(coro)
    assert coro.cr_frame is None, "未 await 的协程应被关闭，避免告警"


# ---------------------------------------------------------------------------
# MinioService
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, data):
        self._data = data
        self.closed = False
        self.released = False

    def read(self):
        return self._data

    def close(self):
        self.closed = True

    def release_conn(self):
        self.released = True


class _FakeMinioClient:
    def __init__(self, stat_error=None, get_error=None):
        self.puts = []
        self.objects = {}
        self.stat_error = stat_error
        self.get_error = get_error
        self.created_bucket = None
        self.removed = []

    def bucket_exists(self, bucket):
        return True

    def make_bucket(self, bucket):
        self.created_bucket = bucket

    def put_object(self, bucket, key, data, length, content_type=None):
        self.puts.append((bucket, key, length, content_type))

    def fget_object(self, bucket, key, path):
        self.objects[path] = (bucket, key)

    def remove_object(self, bucket, key):
        self.removed.append((bucket, key))

    def presigned_get_object(self, bucket, key, expires=None):
        return f"http://minio/{bucket}/{key}?expires={expires}"

    def stat_object(self, bucket, key):
        if self.stat_error:
            raise self.stat_error
        return object()

    def get_object(self, bucket, key):
        if self.get_error:
            raise self.get_error
        return _FakeResponse(b"wikitext")


def _s3_error(code):
    # minio 7.x 签名：S3Error(message, code, resource, request_id, host_id, response)
    return S3Error("message", code, "resource", "request-id", "host-id", None)


def _minio_with(client):
    service = MinioService()
    service._client = client
    return service


def _upload_file(filename="文档 v1.PDF", content=b"data", content_type="application/pdf"):
    return types.SimpleNamespace(
        filename=filename, file=io.BytesIO(content), size=len(content), content_type=content_type
    )


def test_minio_ensure_bucket_creates_when_missing(monkeypatch):
    client = _FakeMinioClient()
    client.bucket_exists = lambda bucket: False
    service = _minio_with(client)
    service._ensure_bucket()
    assert client.created_bucket is not None


def test_minio_upload_download_delete_and_url():
    client = _FakeMinioClient()
    service = _minio_with(client)

    file_key = service.upload_file(_upload_file(), file_id="doc-1")
    assert file_key.endswith("documents/doc-1/source.pdf")
    bucket, key, length, content_type = client.puts[0]
    assert length == 4
    assert content_type == "application/pdf"

    # 无扩展名回退 .bin
    no_ext = service.upload_file(_upload_file(filename="README"), file_id="doc-2")
    assert no_ext.endswith("source.bin")

    service.download_file(file_key, "local/path")
    assert client.objects["local/path"][1] == "documents/doc-1/source.pdf"

    service.delete_file(file_key)
    assert client.removed[-1][1] == "documents/doc-1/source.pdf"

    url = service.get_file_url(file_key, expires=60)
    assert url.startswith("http://minio/")
    assert "expires=0:01:00" in url


def test_minio_file_exists_handles_s3_errors():
    ok = _minio_with(_FakeMinioClient())
    assert ok.file_exists("minio://documents/a.txt") is True

    missing = _minio_with(_FakeMinioClient(stat_error=_s3_error("NoSuchKey")))
    assert missing.file_exists("minio://documents/a.txt") is False

    denied = _minio_with(_FakeMinioClient(stat_error=_s3_error("AccessDenied")))
    with pytest.raises(S3Error):
        denied.file_exists("minio://documents/a.txt")


def test_minio_text_roundtrip_and_missing():
    client = _FakeMinioClient()
    service = _minio_with(client)
    assert service.upload_text("wiki/kb-1/page.md", "# 标题") == "wiki/kb-1/page.md"
    assert client.puts[-1][2] == len("# 标题".encode("utf-8"))
    assert service.download_text("wiki/kb-1/page.md") == "wikitext"

    missing = _minio_with(_FakeMinioClient(get_error=_s3_error("NoSuchKey")))
    assert missing.download_text("wiki/kb-1/page.md") is None

    denied = _minio_with(_FakeMinioClient(get_error=_s3_error("AccessDenied")))
    with pytest.raises(S3Error):
        denied.download_text("wiki/kb-1/page.md")


async def test_minio_async_wrappers():
    client = _FakeMinioClient()
    service = _minio_with(client)

    file_key = await service.upload_file_async(_upload_file(), file_id="doc-3")
    assert file_key.endswith("documents/doc-3/source.pdf")

    await service.download_file_async(file_key, "local/async")
    assert "local/async" in client.objects

    await service.delete_file_async(file_key)
    assert client.removed

    url = await service.get_file_url_async(file_key)
    assert url.startswith("http://minio/")

    assert await service.file_exists_async(file_key) is True

    assert await service.upload_text_async("wiki/a.md", "content") == "wiki/a.md"
    assert await service.download_text_async("wiki/a.md") == "wikitext"


# ---------------------------------------------------------------------------
# OllamaReranker
# ---------------------------------------------------------------------------


class _FakeOllamaClient:
    def __init__(self, host=None, output="7", error=None):
        self.host = host
        self.output = output
        self.error = error
        self.calls = []

    async def generate(self, model, prompt):
        self.calls.append((model, prompt))
        if self.error:
            raise self.error
        return {"response": self.output}


async def test_ollama_reranker_initializes_once(monkeypatch):
    created = []

    def _factory(host=None):
        client = _FakeOllamaClient(host=host)
        created.append(client)
        return client

    monkeypatch.setattr("src.services.ollama_reranker.AsyncClient", _factory)
    reranker = OllamaReranker("init-model")
    assert OllamaReranker("init-model") is reranker

    await reranker._ensure_initialized()
    await reranker._ensure_initialized()
    assert reranker._initialized is True
    assert len(created) == 1


async def test_ollama_reranker_score_pair_and_predict(monkeypatch):
    client = _FakeOllamaClient(output="8")
    monkeypatch.setattr("src.services.ollama_reranker.AsyncClient", lambda host=None: client)

    reranker = OllamaReranker("score-model")
    assert await reranker._score_pair("q", "d") == pytest.approx(0.8)
    assert client.calls[0][0] == "score-model"

    scores = await reranker.predict([("q1", "d1"), ("q2", "d2")])
    assert scores == pytest.approx([0.8, 0.8])
    assert await reranker.predict([]) == []

    failing = _FakeOllamaClient(error=RuntimeError("ollama down"))
    monkeypatch.setattr("src.services.ollama_reranker.AsyncClient", lambda host=None: failing)
    reranker2 = OllamaReranker("failing-model")
    assert await reranker2._score_pair("q", "d") == 0.0
    assert await reranker2.predict([("q", "d")]) == [0.0]


# ---------------------------------------------------------------------------
# VectorStoreManager
# ---------------------------------------------------------------------------


class _FakeMilvus:
    def __init__(self, results=None, hybrid_error=None):
        self.results = results or []
        self.hybrid_error = hybrid_error
        self.calls = []

    def _record(self, name, args, kwargs):
        self.calls.append((name, args, kwargs))

    async def search_hybrid(self, query, **kwargs):
        self._record("search_hybrid", (query,), kwargs)
        if self.hybrid_error:
            raise self.hybrid_error
        return self.results

    async def search(self, query, **kwargs):
        self._record("search", (query,), kwargs)
        return self.results

    async def search_dense(self, query, **kwargs):
        self._record("search_dense", (query,), kwargs)
        return self.results

    async def search_hybrid_multi(self, queries, **kwargs):
        self._record("search_hybrid_multi", (queries,), kwargs)
        return self.results

    async def get_document_chunks(self, document_id):
        self._record("get_document_chunks", (document_id,), {})
        return self.results

    async def insert_embeddings(self, documents, kb_id=""):
        self._record("insert_embeddings", (documents, kb_id), {})

    async def delete_by_document_id(self, document_id):
        self._record("delete_by_document_id", (document_id,), {})

    async def delete_by_kb_id(self, kb_id):
        self._record("delete_by_kb_id", (kb_id,), {})

    async def delete_by_kb_ids(self, kb_ids):
        self._record("delete_by_kb_ids", (kb_ids,), {})

    async def flush(self):
        self._record("flush", (), {})

    async def count(self):
        return 7

    async def count_by_kb(self, kb_id):
        return 3


def _vector_store(milvus):
    store = VectorStoreManager()
    store.milvus_service = milvus
    return store


def test_results_to_documents_maps_metadata_and_score_fallbacks():
    store = _vector_store(_FakeMilvus())
    docs = store._results_to_documents(
        [
            {"content": "a", "rerank_score": 0.9, "document_id": "d1"},
            {"content": "b", "rrf_score": 0.5},
            {"content": "c", "score": 0.2, "source_kind": "wiki", "chunk_index": 3},
        ]
    )
    assert [d.page_content for d in docs] == ["a", "b", "c"]
    assert docs[0].metadata["score"] == 0.9
    assert docs[1].metadata["score"] == 0.5
    assert docs[2].metadata["score"] == 0.2
    assert docs[2].metadata["source_kind"] == "wiki"
    assert docs[2].metadata["chunk_index"] == 3
    assert docs[0].metadata["kb_id"] == ""


async def test_vector_store_search_hybrid_and_fallback():
    milvus = _FakeMilvus(results=[{"content": "x", "score": 1.0}])
    store = _vector_store(milvus)
    docs = await store.search("问题", k=5, kb_ids=["kb-1"])
    assert docs[0].page_content == "x"
    assert milvus.calls[0][0] == "search_hybrid"

    milvus = _FakeMilvus(hybrid_error=MilvusException("hybrid broken"))
    store = _vector_store(milvus)
    docs = await store.search("问题")
    assert [c[0] for c in milvus.calls] == ["search_hybrid", "search"]
    assert docs == []

    milvus = _FakeMilvus(hybrid_error=ValueError("config broken"))
    store = _vector_store(milvus)
    with pytest.raises(ValueError):
        await store.search("问题")


async def test_vector_store_passthrough_wrappers():
    milvus = _FakeMilvus(results=[{"content": "x"}])
    store = _vector_store(milvus)

    docs = await store.search_dense("q", k=2, query_embedding=[0.1], source_kind="raw")
    assert docs[0].page_content == "x"
    assert milvus.calls[-1][0] == "search_dense"
    assert milvus.calls[-1][2]["query_embedding"] == [0.1]

    await store.search_hybrid("q", exclude_source_kinds=["wiki"])
    assert milvus.calls[-1][0] == "search_hybrid"

    await store.search_hybrid_multi(["q1", "q2"])
    assert milvus.calls[-1][0] == "search_hybrid_multi"
    assert milvus.calls[-1][1] == (["q1", "q2"],)

    docs = await store.get_chunks_by_document_id("d1")
    assert docs[0].page_content == "x"

    await store.create_vector_store(["doc"], kb_id="kb-1")
    await store.add_documents(["doc"], kb_id="kb-2")
    await store.delete_by_document_id("d1")
    await store.delete_by_kb_id("kb-1")
    await store.delete_by_kb_ids(["kb-1", "kb-2"])
    await store.flush()
    assert await store.count() == 7
    assert await store.count_by_kb("kb-1") == 3

    names = [c[0] for c in milvus.calls]
    for expected in (
        "insert_embeddings",
        "delete_by_document_id",
        "delete_by_kb_id",
        "delete_by_kb_ids",
        "flush",
    ):
        assert expected in names
    assert names.count("insert_embeddings") == 2
