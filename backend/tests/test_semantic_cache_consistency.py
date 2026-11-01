"""语义缓存一致性：命中条目引用已删文档（CODE-2 第二十五批）。

缓存失效是 best-effort（Redis 抖动、关机丢任务、scope 索引写失败导致按库失效
漏 scope），任何一次漏失效都会让「引用已删/已下架文档的答案」在 TTL（默认 24h）
内继续被回放。本批在命中侧加一道来源校验，并补齐失效链路原本完全静默的分支。
"""

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager

import pytest

from src.services import semantic_cache_service as mod
from src.services.semantic_cache_service import SemanticCacheHit, SemanticCacheService


# ---------------------------------------------------------------------------
# 假对象
# ---------------------------------------------------------------------------
class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class _DB:
    def __init__(self, rows):
        self._rows = list(rows)
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        return _Rows(self._rows)


class _Client:
    def __init__(self):
        self.hdel_calls = []
        self.sadd_calls = []

    async def hdel(self, key, *fields):
        self.hdel_calls.append((key, list(fields)))
        return 1

    async def sadd(self, key, member):
        self.sadd_calls.append((key, member))
        return 1

    async def expire(self, key, ttl):
        return True

    async def hgetall(self, key):
        return {}

    async def hget(self, key, field):
        return None

    async def delete(self, key):
        return 1


class _Cache:
    def __init__(self, available=True):
        self.available = available
        self.client = _Client()


def _hit(metadata, question="缓存里的问题", match_type="exact"):
    return SemanticCacheHit(
        question=question,
        answer="缓存答案",
        source_texts=["片段"],
        source_metadata=metadata,
        score=1.0,
        match_type=match_type,
    )


def _patch_db(monkeypatch, db):
    @asynccontextmanager
    async def _maker():
        yield db

    monkeypatch.setattr("src.database.async_session_maker", _maker)


def _patch_cache(monkeypatch, service, cache):
    async def _get_cache():
        return cache

    monkeypatch.setattr(service, "_get_cache", _get_cache)


# ---------------------------------------------------------------------------
# 1. 来源有效性校验
# ---------------------------------------------------------------------------
class TestHitSourceValidation:
    @pytest.mark.asyncio
    async def test_alive_document_passes(self, monkeypatch):
        """来源文档仍在且已发布 → 命中有效。"""
        kb_id = str(uuid.uuid4())
        doc_id = str(uuid.uuid4())
        db = _DB([(uuid.UUID(doc_id), uuid.UUID(kb_id))])
        _patch_db(monkeypatch, db)
        service = SemanticCacheService()

        hit = _hit([{"source_type": "kb", "document_id": doc_id, "kb_id": kb_id}])
        ok, missing = await service._sources_alive(hit, [kb_id])

        assert ok is True and missing == []

    @pytest.mark.asyncio
    async def test_status_filter_requires_published(self, monkeypatch):
        """下架/归档文档不算有效来源 —— 查询必须带 status 过滤。"""
        kb_id = str(uuid.uuid4())
        doc_id = str(uuid.uuid4())
        db = _DB([])
        _patch_db(monkeypatch, db)
        service = SemanticCacheService()

        hit = _hit([{"source_type": "kb", "document_id": doc_id}])
        ok, missing = await service._sources_alive(hit, [kb_id])

        assert ok is False and missing == [doc_id]
        assert "status" in str(db.statements[0]), "校验必须只看 published 文档"

    @pytest.mark.asyncio
    async def test_document_moved_to_other_kb_is_invalid(self, monkeypatch):
        """文档已被移到别的知识库 → 该 scope 的答案不再适用。"""
        kb_id, other_kb = str(uuid.uuid4()), str(uuid.uuid4())
        doc_id = str(uuid.uuid4())
        _patch_db(monkeypatch, _DB([(uuid.UUID(doc_id), uuid.UUID(other_kb))]))
        service = SemanticCacheService()

        hit = _hit([{"source_type": "kb", "document_id": doc_id}])
        ok, missing = await service._sources_alive(hit, [kb_id])

        assert ok is False and missing == [doc_id]

    @pytest.mark.asyncio
    async def test_wiki_source_checked_against_wiki_pages(self, monkeypatch):
        """wiki 来源的 document_id 是页面 id，不能拿 documents 表判断。"""
        page_id = str(uuid.uuid4())
        db = _DB([(page_id,)])
        _patch_db(monkeypatch, db)
        service = SemanticCacheService()

        hit = _hit([{"source_type": "kb", "document_id": page_id, "source_kind": "wiki"}])
        ok, missing = await service._sources_alive(hit, [str(uuid.uuid4())])

        assert ok is True and missing == []
        assert "wiki_pages" in str(db.statements[0])

    @pytest.mark.asyncio
    async def test_no_document_id_skips_validation(self, monkeypatch):
        """老条目没有 document_id：无从判断，不能因此丢弃缓存。"""
        db = _DB([])
        _patch_db(monkeypatch, db)
        service = SemanticCacheService()

        hit = _hit([{"source_type": "kb", "filename": "a.pdf"}])
        ok, missing = await service._sources_alive(hit, [str(uuid.uuid4())])

        assert ok is True and missing == []
        assert db.statements == []

    @pytest.mark.asyncio
    async def test_db_failure_fails_open(self, monkeypatch):
        """库不可用时缓存是唯一能出答案的路径 → 放行，不能把缓存也打死。"""

        @asynccontextmanager
        async def _boom():
            raise RuntimeError("db down")
            yield

        monkeypatch.setattr("src.database.async_session_maker", _boom)
        service = SemanticCacheService()

        hit = _hit([{"source_type": "kb", "document_id": str(uuid.uuid4())}])
        ok, missing = await service._sources_alive(hit, [str(uuid.uuid4())])

        assert ok is True and missing == []

    @pytest.mark.asyncio
    async def test_validate_hit_drops_entry_and_reports(self, monkeypatch, caplog):
        """来源失效：删条目 + REPORTABLE（说明失效链路漏了一次），返回 False。"""
        service = SemanticCacheService()
        cache = _Cache()
        _patch_cache(monkeypatch, service, cache)
        _patch_db(monkeypatch, _DB([]))
        doc_id = str(uuid.uuid4())
        kb_id = str(uuid.uuid4())
        hit = _hit([{"source_type": "kb", "document_id": doc_id}])

        with caplog.at_level(logging.ERROR, logger=mod.logger.name):
            ok = await service.validate_hit(hit, "u-1", [kb_id])

        assert ok is False
        assert cache.client.hdel_calls, "失效条目必须从 Redis 删除，否则每次命中都要再校验"
        assert cache.client.hdel_calls[0][0] == f"rag:semcache:u-1:{_scope_hash([kb_id])}"
        hits = [r for r in caplog.records if "action=semantic_cache.stale_hit" in r.getMessage()]
        assert hits and hits[0].levelno == logging.ERROR
        assert "missing_document_ids=" + doc_id in hits[0].getMessage()


def _scope_hash(kb_ids):
    import hashlib

    return hashlib.md5(",".join(sorted(kb_ids)).encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# 2. 失效链路原本静默的分支
# ---------------------------------------------------------------------------
class TestInvalidationVisibility:
    @pytest.mark.asyncio
    async def test_invalidate_skipped_when_cache_unavailable_is_logged(
        self, monkeypatch, caplog
    ):
        """Redis 不可用时失效被跳过，此前 return 0 一字不记。"""
        service = SemanticCacheService()
        _patch_cache(monkeypatch, service, _Cache(available=False))

        with caplog.at_level(logging.WARNING, logger=mod.logger.name):
            deleted = await service.invalidate_kb(str(uuid.uuid4()))

        assert deleted == 0
        hits = [
            r for r in caplog.records
            if "action=semantic_cache.invalidate_unavailable" in r.getMessage()
        ]
        assert hits, "跳过失效必须留痕，否则「删了但缓存没清」无从发现"

    @pytest.mark.asyncio
    async def test_index_scope_failure_is_reportable(self, monkeypatch, caplog):
        """scope 索引写失败 → 该范围以后按 kb 失效永远扫不到它。"""
        service = SemanticCacheService()
        cache = _Cache()

        async def _boom(*args, **kwargs):
            raise RuntimeError("redis timeout")

        monkeypatch.setattr(cache.client, "sadd", _boom)

        with caplog.at_level(logging.ERROR, logger=mod.logger.name):
            await service._index_scope(cache, "rag:semcache:u-1:abc", ["kb-1"])

        hits = [r for r in caplog.records if "action=semantic_cache.index_scope" in r.getMessage()]
        assert hits and hits[0].levelno == logging.ERROR

    @pytest.mark.asyncio
    async def test_drain_waits_for_pending_invalidations(self, monkeypatch):
        """关机排空：正常任务等在途完成，超时任务取消并上报。"""

        async def _slow(self, kb_id):
            await asyncio.sleep(5)
            return 1

        monkeypatch.setattr(SemanticCacheService, "invalidate_kb", _slow)
        mod.schedule_invalidation("kb-slow")
        result = await mod.drain_invalidations(timeout=0.01)

        assert result["pending"] == 1
        assert result["cancelled"] == 1

    @pytest.mark.asyncio
    async def test_drain_completes_fast_invalidations(self, monkeypatch):
        async def _fast(self, kb_id):
            return 2

        monkeypatch.setattr(SemanticCacheService, "invalidate_kb", _fast)
        mod.schedule_invalidation("kb-fast")
        result = await mod.drain_invalidations(timeout=5.0)

        assert result == {"pending": 1, "completed": 1, "cancelled": 0}
