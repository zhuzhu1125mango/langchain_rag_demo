"""Wiki 编译去抖调度器单元测试（P5，全 mock）。

覆盖：入队登记、计时器触发合并编译、多 doc_ids 归属、窗口内删除跳过、
失败/超时指标、DEBOUNCE=0 行为。
"""

import asyncio
from types import SimpleNamespace

import pytest

from src.config import settings
from src.services.wiki_compile_scheduler import (
    _pending,
    _timers,
    pending_docs,
    schedule_compile,
    wait_running_tasks,
)
from src.services.wiki_compiler import WikiCompileResult


@pytest.fixture(autouse=True)
def reset_scheduler(monkeypatch):
    """隔离调度器全局状态与去抖时长。"""
    _pending.clear()
    _timers.clear()
    monkeypatch.setattr(settings.wiki_compile, "WIKI_COMPILE_DEBOUNCE_SECONDS", 0)
    yield
    for t in _timers.values():
        t.cancel()
    _pending.clear()
    _timers.clear()


class FakeSession:
    """占位会话：_compile_batch 内对 db 的使用全部被 mock 掉。"""


@pytest.fixture
def fake_session_maker(monkeypatch):
    from contextlib import asynccontextmanager

    calls = {"entered": 0}

    @asynccontextmanager
    async def maker():
        calls["entered"] += 1
        yield FakeSession()

    monkeypatch.setattr("src.database.async_session_maker", maker)
    return calls


def _patch_compile(monkeypatch, result_or_exc):
    """替换 WikiCompiler.compile_document，返回捕获列表 [(kb_id, doc_ids, chunks)]。"""
    calls = []

    async def fake_compile(self, db, kb_id, doc_ids, chunks):
        calls.append({"kb_id": kb_id, "doc_ids": list(doc_ids), "chunks": chunks})
        if isinstance(result_or_exc, Exception):
            raise result_or_exc
        return result_or_exc

    monkeypatch.setattr("src.services.wiki_compiler.WikiCompiler.compile_document", fake_compile)
    return calls


def _patch_load_chunks(monkeypatch, mapping):
    """按 doc_id 返回 raw chunks（未登记的 doc 返回空）。"""

    async def fake_load(kb_id, doc_id):
        return list(mapping.get(doc_id, []))

    monkeypatch.setattr("src.services.wiki_rebuild.load_raw_chunks", fake_load)


def _chunks(*texts):
    return [SimpleNamespace(page_content=t) for t in texts]


class TestScheduleCompile:
    async def test_registers_pending(self):
        schedule_compile("kb-1", "doc-1")
        assert pending_docs("kb-1") == {"doc-1"}

    async def test_accumulates_pending(self):
        schedule_compile("kb-1", "doc-1")
        schedule_compile("kb-1", "doc-2")
        schedule_compile("kb-2", "doc-3")
        assert pending_docs("kb-1") == {"doc-1", "doc-2"}
        assert pending_docs("kb-2") == {"doc-3"}

    async def test_merged_single_compile(self, fake_session_maker, monkeypatch):
        calls = _patch_compile(monkeypatch, WikiCompileResult(pages_created=1))
        _patch_load_chunks(monkeypatch, {"d1": _chunks("甲"), "d2": _chunks("乙")})

        schedule_compile("kb-m", "d1")
        schedule_compile("kb-m", "d2")
        await asyncio.sleep(0.01)  # debounce=0 → 计时器下一拍触发
        await wait_running_tasks(timeout=5)

        assert len(calls) == 1
        assert calls[0]["doc_ids"] == ["d1", "d2"]
        assert [c.page_content for c in calls[0]["chunks"]] == ["甲", "乙"]
        assert pending_docs("kb-m") == set()  # 触发后清空

    async def test_deleted_doc_skipped(self, fake_session_maker, monkeypatch):
        """去抖窗口内被删除的文档（无 raw chunks）自然跳过。"""
        calls = _patch_compile(monkeypatch, WikiCompileResult())
        _patch_load_chunks(monkeypatch, {"d1": _chunks("甲"), "d2": []})

        schedule_compile("kb-del", "d1")
        schedule_compile("kb-del", "d2")
        await asyncio.sleep(0.01)
        await wait_running_tasks(timeout=5)

        assert len(calls) == 1
        assert calls[0]["doc_ids"] == ["d1"]

    async def test_all_chunks_empty_skips_compile(self, fake_session_maker, monkeypatch):
        calls = _patch_compile(monkeypatch, WikiCompileResult())
        _patch_load_chunks(monkeypatch, {"d1": []})

        schedule_compile("kb-empty", "d1")
        await asyncio.sleep(0.01)
        await wait_running_tasks(timeout=5)
        assert calls == []

    async def test_compile_failure_records_metric(self, fake_session_maker, monkeypatch):
        recorded = []
        monkeypatch.setattr(
            "src.middleware.prometheus.record_wiki_compile",
            lambda **kw: recorded.append(kw),
        )
        _patch_compile(monkeypatch, RuntimeError("LLM 炸了"))
        _patch_load_chunks(monkeypatch, {"d1": _chunks("甲")})

        schedule_compile("kb-fail", "d1")
        await asyncio.sleep(0.01)
        await wait_running_tasks(timeout=5)
        assert recorded == [{"result": "failed"}]

    async def test_compile_timeout_records_metric(self, fake_session_maker, monkeypatch):
        recorded = []
        monkeypatch.setattr(
            "src.middleware.prometheus.record_wiki_compile",
            lambda **kw: recorded.append(kw),
        )
        _patch_compile(monkeypatch, asyncio.TimeoutError())
        _patch_load_chunks(monkeypatch, {"d1": _chunks("甲")})

        schedule_compile("kb-timeout", "d1")
        await asyncio.sleep(0.01)
        await wait_running_tasks(timeout=5)
        assert recorded[0]["result"] == "timeout"
