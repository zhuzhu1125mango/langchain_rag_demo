"""语义缓存服务单元测试（P1-3）。

覆盖：精确/语义命中、阈值过滤、TTL 过期、容量淘汰、失效匹配、
fail-open 降级、scope key 稳定性、余弦边界。
"""

import asyncio
import json

import pytest

from src.services.semantic_cache_service import (
    SemanticCacheHit,
    SemanticCacheService,
    schedule_invalidation,
)

# 注意：monkeypatch 的字符串目标不支持「模块.类.方法」跨属性解析，
# 统一 import 类对象后直接 patch（rag_chain 导入的是同一个类对象，patch 全局生效）。


# ----------------------------------------------------------------------
# Fake Redis / Fake CacheService
# ----------------------------------------------------------------------
class FakeRedis:
    """最小异步 Redis HASH 客户端模拟（decode_responses 语义）。"""

    def __init__(self):
        self.store = {}  # key -> {field: json_str}
        self.fail = False

    def _check(self):
        if self.fail:
            raise ConnectionError("redis down")

    async def hset(self, key, field, value):
        self._check()
        self.store.setdefault(key, {})[field] = value
        return 1

    async def hgetall(self, key):
        self._check()
        return dict(self.store.get(key, {}))

    async def hdel(self, key, *fields):
        self._check()
        bucket = self.store.get(key, {})
        deleted = 0
        for f in fields:
            if f in bucket:
                bucket.pop(f)
                deleted += 1
        return deleted

    async def expire(self, key, ttl):
        self._check()
        return True

    async def scan_iter(self, match="*"):
        self._check()
        import fnmatch
        for key in list(self.store.keys()):
            if fnmatch.fnmatch(key, match):
                yield key


class FakeCache:
    def __init__(self, client=None):
        self.client = client or FakeRedis()
        self.available = True

    async def ping(self):
        return True


@pytest.fixture
def fake_cache(monkeypatch):
    cache = FakeCache()

    async def fake_get_instance():
        return cache

    monkeypatch.setattr(
        "src.services.cache_service.CacheService.get_instance", fake_get_instance
    )
    return cache


@pytest.fixture
def service():
    return SemanticCacheService()


def patch_embeddings(monkeypatch, service, vectors):
    """按问题名固定 embedding；未注册的问题返回默认向量 v_default。"""

    async def fake_embed(question: str):
        return vectors.get(question, vectors.get("__default__"))

    monkeypatch.setattr(service, "_embed_question", fake_embed)


# ----------------------------------------------------------------------
# scope key 与余弦
# ----------------------------------------------------------------------
class TestScopeKey:
    def test_same_set_different_order_same_key(self):
        a = SemanticCacheService.scope_key("u1", ["kb-b", "kb-a"])
        b = SemanticCacheService.scope_key("u1", ["kb-a", "kb-b"])
        assert a == b
        assert a.startswith("rag:semcache:u1:")

    def test_different_user_different_key(self):
        assert SemanticCacheService.scope_key("u1", ["kb-a"]) != SemanticCacheService.scope_key(
            "u2", ["kb-a"]
        )

    def test_empty_kb_ids_is_all_scope(self):
        assert SemanticCacheService.scope_key("u1", None).endswith(":all")
        assert SemanticCacheService.scope_key("u1", []).endswith(":all")

    def test_none_user_uses_anonymous_placeholder(self):
        # 调用方保证传入 "anonymous"，这里仅验证 key 构造不抛错
        assert SemanticCacheService.scope_key("anonymous", ["kb"]).startswith("rag:semcache:")


class TestCosine:
    def test_identical_vectors(self):
        assert SemanticCacheService.cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        assert SemanticCacheService.cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)

    def test_zero_vector_returns_zero(self):
        assert SemanticCacheService.cosine_similarity([0, 0], [1, 0]) == 0.0

    def test_mismatched_length_returns_zero(self):
        assert SemanticCacheService.cosine_similarity([1], [1, 2]) == 0.0

    def test_empty_returns_zero(self):
        assert SemanticCacheService.cosine_similarity([], []) == 0.0


# ----------------------------------------------------------------------
# 查找
# ----------------------------------------------------------------------
class TestLookup:
    async def test_exact_hit(self, fake_cache, service, monkeypatch):
        vectors = {"__default__": [1.0, 0.0]}
        patch_embeddings(monkeypatch, service, vectors)
        await service.store("u1", ["kb-a"], "什么是 RAG？", "RAG 是检索增强生成",
                            ["来源"], [{"document_id": "d1"}])

        hit, emb = await service.lookup("u1", ["kb-a"], "  什么是 RAG？  ")
        assert hit is not None
        assert hit.match_type == "exact"
        assert hit.score == 1.0
        assert hit.answer == "RAG 是检索增强生成"
        assert hit.source_texts == ["来源"]

    async def test_exact_hit_case_insensitive(self, fake_cache, service, monkeypatch):
        patch_embeddings(monkeypatch, service, {"__default__": [1.0, 0.0]})
        await service.store("u1", ["kb-a"], "What is RAG?", "answer", [], [])
        hit, _ = await service.lookup("u1", ["kb-a"], "what is rag?")
        assert hit is not None and hit.match_type == "exact"

    async def test_semantic_hit_above_threshold(self, fake_cache, service, monkeypatch):
        patch_embeddings(monkeypatch, service, {
            "__default__": [1.0, 0.0],
        })
        await service.store("u1", ["kb-a"], "RAG 的定义", "answer", [], [])

        # 查询问题 embedding 与缓存向量同向 → 余弦 1.0
        hit, _ = await service.lookup("u1", ["kb-a"], "RAG 是什么意思")
        assert hit is not None
        assert hit.match_type == "semantic"
        assert hit.score >= 0.92

    async def test_miss_below_threshold_returns_embedding_for_reuse(
        self, fake_cache, service, monkeypatch
    ):
        patch_embeddings(monkeypatch, service, {
            "__default__": [1.0, 0.0],
            "无关问题": [0.0, 1.0],
        })
        await service.store("u1", ["kb-a"], "RAG 的定义", "answer", [], [])

        hit, emb = await service.lookup("u1", ["kb-a"], "无关问题")
        assert hit is None
        # 未命中时返回本次 embedding 供 store 复用
        assert emb == [0.0, 1.0]

    async def test_scope_isolation_between_users(self, fake_cache, service, monkeypatch):
        patch_embeddings(monkeypatch, service, {"__default__": [1.0, 0.0]})
        await service.store("u1", ["kb-a"], "问题A", "用户1的答案", [], [])

        hit, _ = await service.lookup("u2", ["kb-a"], "问题A")
        assert hit is None, "跨用户不得命中（防内容泄露）"

    async def test_scope_isolation_between_kb_sets(self, fake_cache, service, monkeypatch):
        patch_embeddings(monkeypatch, service, {"__default__": [1.0, 0.0]})
        await service.store("u1", ["kb-a"], "问题A", "答案", [], [])
        hit, _ = await service.lookup("u1", ["kb-b"], "问题A")
        assert hit is None

    async def test_expired_entry_misses(self, fake_cache, service, monkeypatch):
        # 显式设置依赖配置，确保测试与 .env 文件及其他测试的状态修改隔离
        monkeypatch.setattr(
            "src.services.semantic_cache_service.settings.semantic_cache.SEMANTIC_CACHE_ENABLED",
            True,
        )
        monkeypatch.setattr(
            "src.services.semantic_cache_service.settings.semantic_cache.SEMANTIC_CACHE_TTL_HOURS",
            24,
        )
        patch_embeddings(monkeypatch, service, {"__default__": [1.0, 0.0]})
        await service.store("u1", ["kb-a"], "问题A", "答案", [], [])
        # 直接回拨条目时间戳（Windows time.time() 粒度粗，动态构造过期不可靠）
        key = SemanticCacheService.scope_key("u1", ["kb-a"])
        for field_id, value in fake_cache.client.store[key].items():
            entry = json.loads(value)
            entry["created_at"] -= 25 * 3600  # 超过默认 TTL 24h
            fake_cache.client.store[key][field_id] = json.dumps(entry, ensure_ascii=False)

        hit, _ = await service.lookup("u1", ["kb-a"], "问题A")
        assert hit is None

    async def test_disabled_returns_miss(self, fake_cache, service, monkeypatch):
        monkeypatch.setattr(
            "src.services.semantic_cache_service.settings.semantic_cache.SEMANTIC_CACHE_ENABLED",
            False,
        )
        hit, emb = await service.lookup("u1", ["kb-a"], "问题A")
        assert hit is None and emb is None

    async def test_redis_failure_fails_open(self, service, monkeypatch):
        cache = FakeCache()
        cache.client.fail = True

        async def fake_get_instance():
            return cache

        monkeypatch.setattr(
            "src.services.cache_service.CacheService.get_instance", fake_get_instance
        )
        hit, emb = await service.lookup("u1", ["kb-a"], "问题A")
        assert hit is None and emb is None


# ----------------------------------------------------------------------
# 写入
# ----------------------------------------------------------------------
class TestStore:
    async def test_store_with_reused_embedding_skips_recompute(
        self, fake_cache, service, monkeypatch
    ):
        called = []

        async def fake_embed(question):
            called.append(question)
            return [1.0, 0.0]

        monkeypatch.setattr(service, "_embed_question", fake_embed)
        ok = await service.store("u1", ["kb-a"], "问题A", "答案", [], [], embedding=[0.5, 0.5])
        assert ok is True
        assert called == [], "已提供 embedding 时不得重复计算"

    async def test_store_persists_entry(self, fake_cache, service, monkeypatch):
        patch_embeddings(monkeypatch, service, {"__default__": [1.0, 0.0]})
        await service.store("u1", ["kb-a"], "问题A", "答案", ["s"], [{"document_id": "d"}])
        key = SemanticCacheService.scope_key("u1", ["kb-a"])
        raw = fake_cache.client.store[key]
        assert len(raw) == 1
        entry = json.loads(next(iter(raw.values())))
        assert entry["kb_ids"] == ["kb-a"]
        assert entry["answer_type"] == "knowledge_base"
        assert entry["question_norm"] == "问题a"

    async def test_store_empty_kb_ids_marks_all(self, fake_cache, service, monkeypatch):
        patch_embeddings(monkeypatch, service, {"__default__": [1.0, 0.0]})
        await service.store("u1", [], "问题A", "答案", [], [])
        entry = json.loads(next(iter(
            fake_cache.client.store[SemanticCacheService.scope_key("u1", [])].values()
        )))
        assert entry["kb_ids"] == ["all"]

    async def test_capacity_evicts_oldest(self, fake_cache, service, monkeypatch):
        monkeypatch.setattr(
            "src.services.semantic_cache_service.settings.semantic_cache.SEMANTIC_CACHE_MAX_ENTRIES",
            2,
        )
        patch_embeddings(monkeypatch, service, {
            "最早的问题": [1.0, 0.0, 0.0],
            "较早的问题": [0.0, 1.0, 0.0],
            "最新问题": [0.0, 0.0, 1.0],
            "__default__": [0.0, 0.0, 1.0],
        })
        await service.store("u1", ["kb-a"], "最早的问题", "答案0", [], [])
        await asyncio.sleep(0.01)
        await service.store("u1", ["kb-a"], "较早的问题", "答案1", [], [])
        await asyncio.sleep(0.01)
        await service.store("u1", ["kb-a"], "最新问题", "答案2", [], [])

        # 最早的条目被淘汰，无法精确命中
        hit, _ = await service.lookup("u1", ["kb-a"], "最早的问题")
        assert hit is None
        hit, _ = await service.lookup("u1", ["kb-a"], "最新问题")
        assert hit is not None

    async def test_store_disabled_returns_false(self, fake_cache, service, monkeypatch):
        monkeypatch.setattr(
            "src.services.semantic_cache_service.settings.semantic_cache.SEMANTIC_CACHE_ENABLED",
            False,
        )
        assert await service.store("u1", ["kb-a"], "问题A", "答案", [], []) is False

    async def test_store_redis_failure_fails_open(self, service, monkeypatch):
        cache = FakeCache()
        cache.client.fail = True

        async def fake_get_instance():
            return cache

        monkeypatch.setattr(
            "src.services.cache_service.CacheService.get_instance", fake_get_instance
        )
        patch_embeddings(monkeypatch, service, {"__default__": [1.0, 0.0]})
        assert await service.store("u1", ["kb-a"], "问题A", "答案", [], []) is False


# ----------------------------------------------------------------------
# 失效
# ----------------------------------------------------------------------
class TestInvalidate:
    async def test_invalidate_matches_kb_and_all(self, fake_cache, service, monkeypatch):
        patch_embeddings(monkeypatch, service, {"__default__": [1.0, 0.0]})
        await service.store("u1", ["kb-1"], "问题1", "答案1", [], [])
        await service.store("u1", ["kb-2"], "问题2", "答案2", [], [])
        await service.store("u1", [], "全域问题", "全域答案", [], [])

        deleted = await service.invalidate_kb("kb-1")
        assert deleted == 2, "kb-1 的条目与 all 全域条目都应删除"

        assert (await service.lookup("u1", ["kb-1"], "问题1"))[0] is None
        assert (await service.lookup("u1", [], "全域问题"))[0] is None
        # 无关条目保留
        hit, _ = await service.lookup("u1", ["kb-2"], "问题2")
        assert hit is not None

    async def test_invalidate_no_match_returns_zero(self, fake_cache, service, monkeypatch):
        patch_embeddings(monkeypatch, service, {"__default__": [1.0, 0.0]})
        await service.store("u1", ["kb-2"], "问题2", "答案2", [], [])
        assert await service.invalidate_kb("kb-9") == 0

    async def test_invalidate_redis_failure_returns_zero(self, service, monkeypatch):
        cache = FakeCache()
        cache.client.fail = True

        async def fake_get_instance():
            return cache

        monkeypatch.setattr(
            "src.services.cache_service.CacheService.get_instance", fake_get_instance
        )
        assert await service.invalidate_kb("kb-1") == 0


# ----------------------------------------------------------------------
# 后台失效调度器
# ----------------------------------------------------------------------
class TestScheduleInvalidation:
    def test_schedule_invalidation_is_fire_and_forget(self, fake_cache, service, monkeypatch):
        patch_embeddings(monkeypatch, service, {"__default__": [1.0, 0.0]})

        async def scenario():
            await service.store("u1", ["kb-x"], "问题X", "答案", [], [])
            schedule_invalidation("kb-x")
            await asyncio.sleep(0.05)
            hit, _ = await service.lookup("u1", ["kb-x"], "问题X")
            assert hit is None

        asyncio.run(scenario())

    def test_schedule_invalidation_swallows_errors(self, fake_cache, monkeypatch):
        async def scenario():
            fake_cache.client.fail = True
            schedule_invalidation("kb-x")  # 不应抛出
            await asyncio.sleep(0.05)

        asyncio.run(scenario())
