"""Wiki lint 周期调度单测（全离线）。覆盖：活跃 KB 枚举、单 KB 失败不扩散、间隔 0 不启动。"""

import asyncio
from types import SimpleNamespace

import pytest

import src.services.wiki_lint_scheduler as sched


class FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return FakeScalars(self._rows)


class FakeDB:
    async def execute(self, *_a, **_k):
        return FakeResult(FAKE_KB_IDS)


class FakeSessionMaker:
    def __call__(self):
        return self

    async def __aenter__(self):
        return FakeDB()

    async def __aexit__(self, *_a):
        return False


FAKE_KB_IDS = ["kb-1", "kb-2"]


async def test_lint_active_kbs_counts_and_isolates_failures(monkeypatch):
    """成功计数；单个 KB lint 抛错不影响其余 KB。"""
    calls = []

    async def fake_lint(db, kb_id):
        calls.append(kb_id)
        if kb_id == "kb-1":
            raise RuntimeError("db hiccup")
        return SimpleNamespace(to_dict=lambda: {"issues": []})

    monkeypatch.setattr(sched, "lint_kb_wiki", fake_lint)
    monkeypatch.setattr("src.database.async_session_maker", FakeSessionMaker())

    count = await sched.lint_active_kbs()
    assert count == 1
    assert calls == ["kb-1", "kb-2"]


async def test_lint_active_kbs_empty(monkeypatch):
    """无 active wiki 页时返回 0。"""
    monkeypatch.setattr(sched, "lint_kb_wiki", lambda *a, **k: None)
    monkeypatch.setattr("src.database.async_session_maker", FakeSessionMaker())
    # 构造空结果：FakeDB 返回空行列表
    class EmptyDB:
        async def execute(self, *_a, **_k):
            return FakeResult([])

    class EmptyMaker:
        def __call__(self):
            return self

        async def __aenter__(self):
            return EmptyDB()

        async def __aexit__(self, *_a):
            return False

    monkeypatch.setattr("src.database.async_session_maker", EmptyMaker())
    assert await sched.lint_active_kbs() == 0


async def test_start_scheduler_disabled_when_interval_zero(monkeypatch):
    """WIKI_LINT_INTERVAL_HOURS=0 时不启动循环（仅手动触发）。"""
    from src.config import settings

    monkeypatch.setattr(settings.wiki_compile, "WIKI_LINT_INTERVAL_HOURS", 0)
    assert sched.start_scheduler() is None
