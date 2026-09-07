"""语义缓存管线集成点单元测试（P1-3）。

不启动完整 RAGChain（绕过 __init__），直接驱动插桩方法：
- _stage_semantic_cache_lookup：命中回放 / 各排除分支
- _maybe_store_semantic_cache：写入条件判断
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from src.services.decision_pipeline import DecisionResult, QAMode
from src.services.rag_chain import RAGChain, _PipelineState
from src.services.reasoning import REASONING_STEP_CACHE_HIT
from src.services.semantic_cache_service import SemanticCacheService
from src.services.trace_collector import TraceCollector


def make_chain() -> RAGChain:
    """绕过 __init__ 构造实例：插桩方法仅依赖 _reasoning_payload 静态方法。"""
    return RAGChain.__new__(RAGChain)


def make_state(**overrides) -> _PipelineState:
    """构造命中路径所需的最小管线状态（纯 KB 问答默认态）。"""
    defaults = dict(
        question="RAG 是什么？",
        kb_ids=["kb-a"],
        history=[],
        use_web_search=False,
        search_mode="simple",
        deep_thinking="off",
    )
    defaults.update(overrides)
    state = _PipelineState(**defaults)
    state.trace = TraceCollector()
    state.trace.set_basic(question=state.question, session_id=None, user_id="u1")
    state.user_id = "u1"
    state.resolved_question = "RAG 是什么？"
    state.decision = DecisionResult(
        should_use_kb=True,
        confidence=1.0,
        mode=QAMode.PURE_KB,
        strategy_results={},
        reasoning="test",
    )
    state.intent_decision = SimpleNamespace(needs_realtime=False)
    return state


def make_hit():
    from src.services.semantic_cache_service import SemanticCacheHit
    return SemanticCacheHit(
        question="什么是 RAG？",
        answer="RAG 是检索增强生成。" * 30,  # 超过 120 字符，验证切片回放
        source_texts=["来源1"],
        source_metadata=[{"document_id": "d1", "filename": "a.pdf"}],
        score=0.95,
        match_type="semantic",
    )


def patch_lookup(monkeypatch, hit=None, embedding=None):
    calls = []

    async def fake_lookup(self, user_id, kb_ids, question):
        calls.append({"user_id": user_id, "kb_ids": kb_ids, "question": question})
        return hit, embedding

    monkeypatch.setattr(SemanticCacheService, "lookup", fake_lookup)
    return calls


def patch_store(monkeypatch):
    calls = []

    async def fake_store(self, **kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(SemanticCacheService, "store", fake_store)
    return calls


# ----------------------------------------------------------------------
# 查找阶段
# ----------------------------------------------------------------------
class TestLookupStage:
    async def test_hit_replays_answer_and_short_circuits(self, monkeypatch):
        chain = make_chain()
        state = make_state()
        calls = patch_lookup(monkeypatch, hit=make_hit())

        events = []
        async for ev in chain._stage_semantic_cache_lookup(state):
            events.append(ev)

        assert len(calls) == 1
        assert calls[0]["user_id"] == "u1"
        assert calls[0]["kb_ids"] == ["kb-a"]
        # 命中标记 + trace 数据
        assert state.finished is True
        assert state.semantic_cache_hit is True
        assert state.trace.data["semantic_cache_hit"]["score"] == 0.95
        # 事件序列：1 条 reasoning + N 条 chunk
        reasoning_events = [e for e in events if e[0] == "reasoning"]
        chunk_events = [e for e in events if e[0] == "chunk"]
        assert len(reasoning_events) == 1
        payload = json.loads(reasoning_events[0][1])
        assert payload["step"] == REASONING_STEP_CACHE_HIT
        assert payload["status"] == "done"
        assert "相似度" in payload["content"]
        # chunk 回放完整答案，事件结构与正常流式一致
        assert "".join(e[1] for e in chunk_events) == make_hit().answer
        assert all(e[4] == "knowledge_base" for e in chunk_events)
        assert chunk_events[0][2] == ["来源1"]
        assert state.final_answer == make_hit().answer
        assert state.answer_type == "knowledge_base"

    async def test_miss_keeps_pipeline_running(self, monkeypatch):
        chain = make_chain()
        state = make_state()
        patch_lookup(monkeypatch, hit=None, embedding=[0.1, 0.2])

        events = []
        async for ev in chain._stage_semantic_cache_lookup(state):
            events.append(ev)

        assert events == []
        assert state.finished is False
        assert state.semantic_cache_embedding == [0.1, 0.2], "未命中时 embedding 应复用给写入"

    async def test_skipped_when_mode_not_pure_kb(self, monkeypatch):
        chain = make_chain()
        state = make_state()
        state.decision = DecisionResult(
            should_use_kb=False, confidence=1.0, mode=QAMode.FUNCTION_CALLING,
            strategy_results={}, reasoning="test",
        )
        calls = patch_lookup(monkeypatch)
        async for _ in chain._stage_semantic_cache_lookup(state):
            pass
        assert calls == []

    async def test_skipped_when_web_search_on(self, monkeypatch):
        chain = make_chain()
        state = make_state(use_web_search=True)
        calls = patch_lookup(monkeypatch)
        async for _ in chain._stage_semantic_cache_lookup(state):
            pass
        assert calls == []

    async def test_skipped_when_deep_thinking_on(self, monkeypatch):
        chain = make_chain()
        state = make_state(deep_thinking="on")
        calls = patch_lookup(monkeypatch)
        async for _ in chain._stage_semantic_cache_lookup(state):
            pass
        assert calls == []

    async def test_skipped_when_no_kb_selected(self, monkeypatch):
        chain = make_chain()
        state = make_state(kb_ids=[])
        calls = patch_lookup(monkeypatch)
        async for _ in chain._stage_semantic_cache_lookup(state):
            pass
        assert calls == []

    async def test_skipped_when_realtime_question(self, monkeypatch):
        chain = make_chain()
        state = make_state()
        state.intent_decision = SimpleNamespace(needs_realtime=True)
        calls = patch_lookup(monkeypatch)
        async for _ in chain._stage_semantic_cache_lookup(state):
            pass
        assert calls == []

    async def test_skipped_when_disabled(self, monkeypatch):
        monkeypatch.setattr(
            "src.services.rag_chain.settings.semantic_cache.SEMANTIC_CACHE_ENABLED", False
        )
        chain = make_chain()
        state = make_state()
        calls = patch_lookup(monkeypatch)
        async for _ in chain._stage_semantic_cache_lookup(state):
            pass
        assert calls == []


# ----------------------------------------------------------------------
# 写入条件
# ----------------------------------------------------------------------
def make_storable_state(**overrides) -> _PipelineState:
    """构造满足全部写入条件的状态（真实 KB 命中答案）。"""
    state = make_state(**overrides)
    state.answer_type = "knowledge_base"
    state.final_answer = "完整答案"
    state.docs = [SimpleNamespace(page_content="chunk")]
    state.semantic_cache_embedding = [1.0, 0.0]
    return state


class TestStoreConditions:
    async def test_storable_state_triggers_store(self, monkeypatch):
        chain = make_chain()
        state = make_storable_state()
        calls = patch_store(monkeypatch)

        chain._maybe_store_semantic_cache(state)
        await asyncio.sleep(0.01)

        assert len(calls) == 1
        assert calls[0]["user_id"] == "u1"
        assert calls[0]["kb_ids"] == ["kb-a"]
        assert calls[0]["answer"] == "完整答案"
        assert calls[0]["embedding"] == [1.0, 0.0]

    async def test_no_store_on_cache_hit(self, monkeypatch):
        chain = make_chain()
        state = make_storable_state()
        state.semantic_cache_hit = True
        calls = patch_store(monkeypatch)
        chain._maybe_store_semantic_cache(state)
        await asyncio.sleep(0.01)
        assert calls == [], "命中回放的答案不应重复写入"

    async def test_no_store_for_non_kb_answer_type(self, monkeypatch):
        chain = make_chain()
        state = make_storable_state()
        state.answer_type = "llm_direct"
        calls = patch_store(monkeypatch)
        chain._maybe_store_semantic_cache(state)
        await asyncio.sleep(0.01)
        assert calls == []

    async def test_no_store_with_web_sources(self, monkeypatch):
        chain = make_chain()
        state = make_storable_state()
        state.web_sources_for_citation = [{"title": "网页"}]
        calls = patch_store(monkeypatch)
        chain._maybe_store_semantic_cache(state)
        await asyncio.sleep(0.01)
        assert calls == []

    async def test_no_store_for_tool_mode(self, monkeypatch):
        chain = make_chain()
        state = make_storable_state()
        state.decision = DecisionResult(
            should_use_kb=True, confidence=1.0, mode=QAMode.FUNCTION_CALLING,
            strategy_results={}, reasoning="test",
        )
        calls = patch_store(monkeypatch)
        chain._maybe_store_semantic_cache(state)
        await asyncio.sleep(0.01)
        assert calls == []

    async def test_no_store_when_deep_thinking_on(self, monkeypatch):
        chain = make_chain()
        state = make_storable_state(deep_thinking="on")
        calls = patch_store(monkeypatch)
        chain._maybe_store_semantic_cache(state)
        await asyncio.sleep(0.01)
        assert calls == []

    async def test_no_store_without_docs(self, monkeypatch):
        chain = make_chain()
        state = make_storable_state()
        state.docs = []
        calls = patch_store(monkeypatch)
        chain._maybe_store_semantic_cache(state)
        await asyncio.sleep(0.01)
        assert calls == []

    async def test_no_store_when_disabled(self, monkeypatch):
        monkeypatch.setattr(
            "src.services.rag_chain.settings.semantic_cache.SEMANTIC_CACHE_ENABLED", False
        )
        chain = make_chain()
        state = make_storable_state()
        calls = patch_store(monkeypatch)
        chain._maybe_store_semantic_cache(state)
        await asyncio.sleep(0.01)
        assert calls == []
