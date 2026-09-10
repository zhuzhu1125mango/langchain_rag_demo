"""Wiki KB 双层锁单元测试（P5，全 mock，无真实 Redis 依赖）。

覆盖：进程内锁互斥、分布式锁关闭时零 Redis 依赖、Redis 不可用降级、
正常加锁/释放（Lua token）、获取超时。
"""

import asyncio

import pytest

from src.config import settings
from src.services.wiki_lock import LOCK_KEY_PREFIX, get_kb_lock, kb_wiki_lock


@pytest.fixture(autouse=True)
def lock_settings(monkeypatch):
    """默认关闭分布式锁，测试按需覆盖。"""
    monkeypatch.setattr(settings.wiki_compile, "WIKI_DISTRIBUTED_LOCK", False)
    monkeypatch.setattr(settings.wiki_compile, "WIKI_COMPILE_TIMEOUT_SECONDS", 300)


class TestInProcessLock:
    def test_same_kb_same_lock(self):
        assert get_kb_lock("kb-x") is get_kb_lock("kb-x")
        assert get_kb_lock("kb-x") is not get_kb_lock("kb-y")

    async def test_disabled_makes_no_redis_calls(self, monkeypatch):
        """WIKI_DISTRIBUTED_LOCK=false 时不得触碰 CacheService。"""
        monkeypatch.setattr(settings.wiki_compile, "WIKI_DISTRIBUTED_LOCK", False)

        class Boom:
            @staticmethod
            async def get_instance():
                raise AssertionError("锁关闭时不得访问 Redis")

        monkeypatch.setattr("src.services.cache_service.CacheService", Boom)
        async with kb_wiki_lock("kb-no-redis"):
            pass  # 正常进入/退出即通过


class FakeRedis:
    def __init__(self, set_result=True):
        self.set_result = set_result
        self.set_calls = []
        self.eval_calls = []

    async def set(self, key, value, nx=False, px=None):
        self.set_calls.append((key, value, nx, px))
        return self.set_result

    async def eval(self, script, numkeys, key, token):
        self.eval_calls.append((key, token))
        return 1


class TestDistributedLock:
    async def test_enabled_acquires_and_releases(self, monkeypatch):
        monkeypatch.setattr(settings.wiki_compile, "WIKI_DISTRIBUTED_LOCK", True)
        fake_redis = FakeRedis(set_result=True)

        async def fake_get_client():
            return fake_redis

        monkeypatch.setattr("src.services.wiki_lock._get_redis_client", fake_get_client)
        inside = []
        async with kb_wiki_lock("kb-dist-1"):
            inside.append(True)
        assert inside == [True]
        # 加锁为 SET NX PX，释放走 Lua compare-token-del
        key, token, nx, px = fake_redis.set_calls[0]
        assert key == LOCK_KEY_PREFIX + "kb-dist-1"
        assert nx is True and px == (300 + 60) * 1000
        assert fake_redis.eval_calls == [(key, token)]

    async def test_redis_unavailable_degrades(self, monkeypatch):
        """CacheService 不可用 → 降级进程内锁，业务正常执行。"""
        monkeypatch.setattr(settings.wiki_compile, "WIKI_DISTRIBUTED_LOCK", True)

        async def boom():
            raise ConnectionError("redis down")

        monkeypatch.setattr("src.services.wiki_lock._get_redis_client", boom)
        ran = []
        async with kb_wiki_lock("kb-degrade"):
            ran.append(True)
        assert ran == [True]

    async def test_set_operation_failure_degrades(self, monkeypatch):
        """Redis SET 抛错 → 降级而非传播。"""
        monkeypatch.setattr(settings.wiki_compile, "WIKI_DISTRIBUTED_LOCK", True)

        class ExplodingRedis:
            async def set(self, *a, **k):
                raise RuntimeError("network error")

        async def fake_get_client():
            return ExplodingRedis()

        monkeypatch.setattr("src.services.wiki_lock._get_redis_client", fake_get_client)
        ran = []
        async with kb_wiki_lock("kb-explode"):
            ran.append(True)
        assert ran == [True]

    async def test_acquire_timeout_raises(self, monkeypatch):
        """锁被他人持有且等待超时 → TimeoutError（与编译外层超时语义对齐）。"""
        monkeypatch.setattr(settings.wiki_compile, "WIKI_DISTRIBUTED_LOCK", True)
        monkeypatch.setattr(settings.wiki_compile, "WIKI_COMPILE_TIMEOUT_SECONDS", 0)
        fake_redis = FakeRedis(set_result=False)  # 永远抢不到

        async def fake_get_client():
            return fake_redis

        monkeypatch.setattr("src.services.wiki_lock._get_redis_client", fake_get_client)
        with pytest.raises(asyncio.TimeoutError):
            async with kb_wiki_lock("kb-timeout"):
                pass

    async def test_mutual_exclusion_within_process(self):
        """同 KB 进程内锁互斥：持锁期间他人无法进入。"""
        order = []

        async def worker(name):
            async with kb_wiki_lock("kb-mutex"):
                order.append(f"{name}-in")
                await asyncio.sleep(0.01)
                order.append(f"{name}-out")

        await asyncio.gather(worker("a"), worker("b"))
        # 串行执行：in/out 必须成对相邻
        assert order[0].split("-")[1] == "in" and order[1] == order[0].replace("in", "out")
