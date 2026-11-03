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


# ---------------------------------------------------------------------------
# 3. C12：删空 scope 后必须把索引成员摘掉
# ---------------------------------------------------------------------------
class _SetClient:
    """能表达 hash 与 set 语义的假客户端（只实现被用到的那几个命令）。

    与上面 `_Client` 的区别：那个只记录调用，无法表达「HDEL 最后一个字段时
    Redis 会删掉整个 key」这一语义——而 C12 正是栽在这个语义上。
    """

    def __init__(self):
        self.hashes = {}
        self.sets = {}
        self.srem_calls = []

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    async def hdel(self, key, *fields):
        bucket = self.hashes.get(key, {})
        for field in fields:
            bucket.pop(field, None)
        # 真实 Redis：hash 变空即删除该 key
        if not bucket:
            self.hashes.pop(key, None)
        return len(fields)

    async def hlen(self, key):
        return len(self.hashes.get(key, {}))

    async def sadd(self, key, *members):
        self.sets.setdefault(key, set()).update(members)
        return len(members)

    async def srem(self, key, *members):
        self.srem_calls.append((key, list(members)))
        bucket = self.sets.get(key, set())
        for member in members:
            bucket.discard(member)
        return len(members)

    async def smembers(self, key):
        return set(self.sets.get(key, set()))

    async def scard(self, key):
        return len(self.sets.get(key, set()))

    async def delete(self, key):
        self.hashes.pop(key, None)
        self.sets.pop(key, None)
        return 1

    async def expire(self, key, ttl):
        return True

    async def exists(self, key):
        return 1 if (key in self.hashes or key in self.sets) else 0

    async def scan_iter(self, match=None, count=None):
        prefix = (match or "").rstrip("*")
        for key in list(self.hashes) + list(self.sets):
            if not match or key.startswith(prefix):
                yield key


class _SetCache:
    available = True

    def __init__(self):
        self.client = _SetClient()


def _json_entry(kb_ids, question="q", created_at=None):
    import json
    import time as _time

    # 用 `is None` 而不是 `or`：created_at=0 是「很久以前」，`or` 会把它当缺省值
    stamp = _time.time() if created_at is None else created_at
    return json.dumps(
        {"kb_ids": kb_ids, "question": question, "created_at": stamp},
        ensure_ascii=False,
    )


class TestIndexPruning:
    @pytest.mark.asyncio
    async def test_invalidate_kb_prunes_index_when_scope_emptied(self, monkeypatch):
        """删空 scope 后索引成员必须摘掉，否则巡检长期报 dangling_cache_index（C12）。"""
        service = SemanticCacheService()
        cache = _SetCache()
        _patch_cache(monkeypatch, service, cache)
        monkeypatch.setattr(mod, "_decode_entry", __import__("json").loads)

        kb_id = "kb-1"
        scope = f"rag:semcache:u-1:{_scope_hash([kb_id])}"
        index_kb = f"{mod.INDEX_PREFIX}:kb:{kb_id}"
        cache.client.hashes[scope] = {"exact:a": _json_entry([kb_id])}
        cache.client.sets[index_kb] = {scope}
        cache.client.sets[mod.INDEX_ALL] = {scope, "rag:semcache:u-2:other"}

        deleted = await service.invalidate_kb(kb_id)

        assert deleted == 1
        assert scope not in cache.client.sets.get(index_kb, set())
        assert scope not in cache.client.sets[mod.INDEX_ALL]
        assert index_kb not in cache.client.sets, "空索引键应一并删除，否则每次失效都为它白跑 HGETALL"
        assert mod.INDEX_ALL in cache.client.sets, "还有别的 scope 时不能删索引键"

    @pytest.mark.asyncio
    async def test_invalidate_kb_keeps_index_when_scope_survives(self, monkeypatch):
        """scope 里还有别的条目不删时，索引成员必须保留——误删就等于以后按库失效永远扫不到它。"""
        service = SemanticCacheService()
        cache = _SetCache()
        _patch_cache(monkeypatch, service, cache)
        monkeypatch.setattr(mod, "_decode_entry", __import__("json").loads)

        kb_id = "kb-1"
        scope = f"rag:semcache:u-1:{_scope_hash([kb_id])}"
        index_kb = f"{mod.INDEX_PREFIX}:kb:{kb_id}"
        cache.client.hashes[scope] = {
            "exact:a": _json_entry([kb_id]),
            "exact:b": _json_entry(["kb-2"]),
        }
        cache.client.sets[index_kb] = {scope}

        deleted = await service.invalidate_kb(kb_id)

        assert deleted == 1
        assert scope in cache.client.sets[index_kb]
        assert not cache.client.srem_calls, "没有删空就不该碰索引"

    @pytest.mark.asyncio
    async def test_trim_prunes_index_when_all_entries_expired(self, monkeypatch):
        """写时惰性清理把 scope 清空后，同样要摘索引成员。"""
        service = SemanticCacheService()
        cache = _SetCache()
        _patch_cache(monkeypatch, service, cache)
        monkeypatch.setattr(mod, "_decode_entry", __import__("json").loads)

        scope = "rag:semcache:u-1:expired"
        index_kb = f"{mod.INDEX_PREFIX}:kb:kb-9"
        cache.client.hashes[scope] = {"exact:old": _json_entry(["kb-9"], created_at=0)}
        cache.client.sets[index_kb] = {scope}

        await service._trim(cache, scope)

        assert scope not in cache.client.hashes
        assert index_kb not in cache.client.sets

    @pytest.mark.asyncio
    async def test_prune_failure_is_recoverable_not_fatal(self, monkeypatch, caplog):
        """摘索引失败不能把「条目已删」这个主结果变成失败：日志要能区分这两种情况。"""
        service = SemanticCacheService()
        cache = _SetCache()
        _patch_cache(monkeypatch, service, cache)
        monkeypatch.setattr(mod, "_decode_entry", __import__("json").loads)

        async def _boom(*args, **kwargs):
            raise RuntimeError("redis srem timeout")

        monkeypatch.setattr(cache.client, "srem", _boom)

        kb_id = "kb-1"
        scope = f"rag:semcache:u-1:{_scope_hash([kb_id])}"
        cache.client.hashes[scope] = {"exact:a": _json_entry([kb_id])}
        cache.client.sets[f"{mod.INDEX_PREFIX}:kb:{kb_id}"] = {scope}

        with caplog.at_level(logging.WARNING, logger=mod.logger.name):
            deleted = await service.invalidate_kb(kb_id)

        assert deleted == 1, "条目确实删掉了，不能因为索引清理失败就报 0"
        hits = [r for r in caplog.records if "action=semantic_cache.prune_index" in r.getMessage()]
        assert hits, "索引残留必须留痕，否则巡检报出来时无从知道是谁留下的"
        assert hits[0].levelno == logging.WARNING
