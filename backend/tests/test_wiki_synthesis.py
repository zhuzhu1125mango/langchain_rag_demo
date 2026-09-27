"""答案回流（synthesis 页）服务单测（全离线，mock 全部外部依赖）。

覆盖阶段一 D3：触发判定真值表、语义去重跳过、缓冲→刷写→持久化链路。
"""

import asyncio
from types import SimpleNamespace

import pytest

import src.database as database_module
import src.services.wiki_synthesis_service as svc_module
from src.services.wiki_synthesis_service import (
    WikiSynthesisService,
    should_synthesize,
)


class FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return FakeScalars(self._rows)


class FakeDB:
    async def execute(self, *_a, **_k):
        return FakeResult(["考勤管理", "远程办公"])


class FakeSessionMaker:
    def __call__(self):
        return self

    async def __aenter__(self):
        return FakeDB()

    async def __aexit__(self, *_a):
        return False


class FakeCompiler:
    def __init__(self):
        self.compile_calls = []
        self.persist_calls = []
        self.page = SimpleNamespace(title="综合：考勤与远程办公", page_type="synthesis", content="# 综合")

    async def compile_synthesis_page(self, question, answer, cited_texts, existing_titles):
        self.compile_calls.append({"question": question, "existing": list(existing_titles)})
        return self.page

    async def persist_pages(self, db, kb_id, doc_ids, compiled):
        self.persist_calls.append({"kb_id": kb_id, "doc_ids": list(doc_ids), "page": compiled})
        return SimpleNamespace(pages_created=1, pages_updated=0)


def _meta(doc_id, kind="raw"):
    return {"document_id": doc_id, "source_kind": kind}


def test_should_synthesize_truth_table():
    """触发判定：类型/长度/引用数/wiki 命中各条件独立生效。"""
    long_answer = "答案" * 120  # 240 字
    # 正例：knowledge_base + 长答案 + 2 个引用文档
    assert should_synthesize("knowledge_base", long_answer, [_meta("d1"), _meta("d2")], False)
    # 正例：引用含 wiki 页（单文档也允许）
    assert should_synthesize("knowledge_base", long_answer, [_meta("d1", "wiki")], False)
    # 反例：联网 / 工具 / Agent 答案不回流
    assert not should_synthesize("web_search", long_answer, [_meta("d1"), _meta("d2")], False)
    assert not should_synthesize("knowledge_base", long_answer, [_meta("d1"), _meta("d2")], True)
    assert not should_synthesize("tool_first", long_answer, [_meta("d1"), _meta("d2")], False)
    # 反例：答案太短
    assert not should_synthesize("knowledge_base", "太短", [_meta("d1"), _meta("d2")], False)
    # 反例：单文档且无 wiki 命中
    assert not should_synthesize("knowledge_base", long_answer, [_meta("d1")], False)


async def test_schedule_skips_when_similar_synthesis_exists(monkeypatch):
    """语义去重：已存在相近 synthesis 页时不进缓冲。"""
    service = WikiSynthesisService(debounce_seconds=0)

    async def _exists(kb_ids, question):
        return True

    monkeypatch.setattr(service, "_similar_synthesis_exists", _exists)
    await service.schedule(["kb-1"], "考勤问题", "答案" * 120, [], [_meta("d1"), _meta("d2")])
    assert service._buffers == {}


async def test_schedule_dedupes_fail_open_and_buffers(monkeypatch):
    """去重检索异常时 fail-open：正常缓冲等待刷写。"""
    import src.services.kb_retrieval_service as kbr_module

    class BoomService:
        def __init__(self, *a, **k):
            pass

        async def retrieve(self, *a, **k):
            raise RuntimeError("redis down")

    monkeypatch.setattr(kbr_module, "KBRetrievalService", BoomService)
    service = WikiSynthesisService(debounce_seconds=3600)
    await service.schedule(["kb-1"], "考勤问题", "答案" * 120, ["片段"], [_meta("d1"), _meta("d2")])
    assert len(service._buffers["kb-1"]) == 1
    # 取消挂起的刷写任务，避免测试遗留
    task = service._tasks.pop("kb-1", None)
    if task:
        task.cancel()


async def test_flush_compiles_and_persists(monkeypatch):
    """刷写：编译 synthesis 页 → persist_pages（引用文档作为 source_doc_ids）。"""
    compiler = FakeCompiler()
    service = WikiSynthesisService(compiler=compiler, debounce_seconds=0)
    monkeypatch.setattr(database_module, "async_session_maker", FakeSessionMaker())

    items = [{
        "kb_id": "kb-1",
        "question": "远程办公期间漏打卡会被扣钱吗？",
        "answer": "答案" * 120,
        "source_texts": ["片段一", "片段二"],
        "doc_ids": ["d1", "d2"],
    }]
    await service.flush("kb-1", items)

    assert len(compiler.compile_calls) == 1
    assert compiler.compile_calls[0]["question"].startswith("远程办公")
    assert "考勤管理" in compiler.compile_calls[0]["existing"]
    assert compiler.persist_calls[0]["kb_id"] == "kb-1"
    assert compiler.persist_calls[0]["doc_ids"] == ["d1", "d2"]
    assert compiler.persist_calls[0]["page"][0].page_type == "synthesis"


async def test_flush_swallows_compiler_errors(monkeypatch):
    """编译失败只记日志，不向管线抛异常（best-effort）。"""
    class BoomCompiler:
        async def compile_synthesis_page(self, **kwargs):
            raise RuntimeError("llm down")

        async def persist_pages(self, *a, **k):  # pragma: no cover
            raise AssertionError("不应到达")

    service = WikiSynthesisService(compiler=BoomCompiler(), debounce_seconds=0)
    monkeypatch.setattr(database_module, "async_session_maker", FakeSessionMaker())
    await service.flush("kb-1", [{
        "kb_id": "kb-1", "question": "q", "answer": "a", "source_texts": [], "doc_ids": [],
    }])


async def test_maybe_schedule_synthesis_respects_enabled_flag(monkeypatch):
    """总开关关闭时不做任何事；开启且条件满足时调用 service.schedule。"""
    from src.config import settings
    from src.services.wiki_synthesis_service import maybe_schedule_synthesis

    state = SimpleNamespace(
        kb_ids=["kb-1"],
        answer_type="knowledge_base",
        final_answer="答案" * 120,
        source_metadata=[_meta("d1"), _meta("d2")],
        source_texts=["片段"],
        web_sources_for_citation=[],
        search_context="",
        resolved_question="考勤问题",
    )
    monkeypatch.setattr(settings.wiki_compile, "WIKI_SYNTHESIS_ENABLED", False)
    await maybe_schedule_synthesis(state)  # 不应抛错、不调度

    monkeypatch.setattr(settings.wiki_compile, "WIKI_SYNTHESIS_ENABLED", True)
    called = {}

    class FakeService:
        async def schedule(self, **kwargs):
            called.update(kwargs)

    monkeypatch.setattr(svc_module, "wiki_synthesis_service", FakeService())
    await maybe_schedule_synthesis(state)
    assert called["kb_ids"] == ["kb-1"]
    assert called["question"] == "考勤问题"
