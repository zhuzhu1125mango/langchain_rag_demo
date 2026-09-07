"""
P1-1 回归测试：rag_chain 流式/非流式统一重构。

验证两件事：
1. _stream_with_retry 续传式重试：中途失败后携带已输出内容作为续写上下文，
   仅产出新增片段（用户不看到重复内容）；重试耗尽时产出 error 事件。
2. run()/arun_stream() 消费同一 _pipeline：流式 chunk 拼接结果与非流式
   完整回答一致，answer_type 一致，reasoning 事件仅在流式对外暴露。
"""

import asyncio
from types import SimpleNamespace

import pytest

import src.services.rag_chain as rag_chain_module
from src.services.rag_chain import RAGChain
from src.services.intent_router.models import PrimaryMode, SearchPipeline
from src.services.trace_collector import TraceCollector


class FakeChunk:
    def __init__(self, content, additional_kwargs=None):
        self.content = content
        self.additional_kwargs = additional_kwargs or {}


class FakeLLM:
    """按脚本依次响应 astream 调用，记录收到的 prompt。

    scripts: 每次调用消耗一个元素 (chunks, error)；
    依次产出 chunks 中各片段后抛出 error（None 表示正常结束）。
    chunks 元素可为 FakeChunk（携带 additional_kwargs 模拟思考增量）或字符串。
    """

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.prompts = []

    async def astream(self, prompt):
        self.prompts.append(prompt)
        chunks, error = self.scripts.pop(0)
        for c in chunks:
            await asyncio.sleep(0)
            yield c if isinstance(c, FakeChunk) else FakeChunk(c)
        if error is not None:
            raise error


def _make_chain(llm):
    """构造不触发外部依赖的 RAGChain，注入最小 mock 依赖。"""
    chain = RAGChain(vector_store=None, strategy_manager=None)
    chain.llm = llm
    # 深度思考关闭（think=False）时 _stream_with_retry 切换到非思考专用模型；
    # 测试脚本统一注入同一 FakeLLM，保持新旧路径行为一致
    chain.llm_direct = llm

    async def _enhance(question, history):
        return {"resolved_question": question, "enhanced_context": ""}

    async def _summary(history):
        return ""

    chain.context_enhancer = SimpleNamespace(
        enhance_context=_enhance,
        _update_conversation_summary=_summary,
    )

    async def _route(question, kb_ids=None, use_web_search=False,
                     search_mode="simple", history=None):
        decision = SimpleNamespace(
            primary_mode=PrimaryMode.DIRECT_LLM,
            suggested_tools=[],
            reasoning="直接回答",
            search_pipeline=SearchPipeline.FAST_PATH,
            needs_realtime=False,
            context_rewrite="",
        )
        decision.to_dict = lambda: {"primary_mode": decision.primary_mode.value}
        return decision

    chain.intent_router = SimpleNamespace(route=_route)

    # ContextBuilder 未注入检索来源时返回空上下文 → 走 llm_direct 路径
    chain.context_builder = SimpleNamespace(
        build_context=lambda question, kb_docs, web_sources: ("", [])
    )
    return chain


@pytest.fixture
def hermetic(monkeypatch):
    """隔离外部副作用：Prometheus、日期工具、trace 落盘、重试等待。"""
    monkeypatch.setattr(rag_chain_module, "PROMETHEUS_AVAILABLE", False)
    monkeypatch.setattr(rag_chain_module, "build_datetime_answer", lambda q: None)
    monkeypatch.setattr(TraceCollector, "save_background", lambda self: None)

    async def _instant_sleep(_seconds):
        await asyncio.sleep(0)

    monkeypatch.setattr(
        rag_chain_module, "asyncio", SimpleNamespace(sleep=_instant_sleep)
    )


class TestStreamRetryResume:
    """续传式重试：失败后仅产出新增内容，无重复输出。"""

    async def test_resume_after_failure_no_duplicate(self, hermetic):
        """第 1 次输出"第一段第二"后中断，重试仅补出剩余部分。"""
        llm = FakeLLM([
            (["第一段", "第二"], RuntimeError("连接中断")),
            (["段", "，结束"], None),
        ])
        chain = _make_chain(llm)

        chunks = []
        async for chunk, st, sm, at in chain._stream_with_retry("PROMPT", [], [], "llm_direct"):
            chunks.append(chunk)

        assert "".join(chunks) == "第一段第二段，结束"
        assert chunks.count("第二") == 1  # 已输出片段不重复

    async def test_retry_prompt_carries_yielded_context(self, hermetic):
        """重试时的 prompt 携带原始提示词与已输出内容作为续写上下文。"""
        llm = FakeLLM([
            (["已输出"], RuntimeError("中断")),
            (["继续"], None),
        ])
        chain = _make_chain(llm)

        async for _ in chain._stream_with_retry("原始问题", [], [], "llm_direct"):
            pass

        assert len(llm.prompts) == 2
        assert llm.prompts[0] == "原始问题"
        assert llm.prompts[1].startswith("原始问题")
        assert "已输出" in llm.prompts[1]

    async def test_success_first_attempt_no_retry(self, hermetic):
        """首次成功不触发重试，prompt 不携带续写上下文。"""
        llm = FakeLLM([(["A", "B"], None)])
        chain = _make_chain(llm)

        chunks = []
        async for chunk, _, _, at in chain._stream_with_retry("PROMPT", [], [], "llm_direct"):
            chunks.append((chunk, at))

        assert chunks == [("A", "llm_direct"), ("B", "llm_direct")]
        assert llm.prompts == ["PROMPT"]

    async def test_retry_exhausted_emits_error_event(self, hermetic):
        """重试耗尽后产出 error 事件，已产出片段保留、无重复。"""
        err = RuntimeError("ollama down")
        llm = FakeLLM([
            (["部分"], err),
            ([], err),
        ])
        chain = _make_chain(llm)

        results = []
        async for chunk, _, _, at in chain._stream_with_retry("P", [], [], "llm_direct"):
            results.append((chunk, at))

        assert results == [
            ("部分", "llm_direct"),
            ("模型调用失败: ollama down", "error"),
        ]

    async def test_thinking_chunks_forwarded_as_sentinel(self, hermetic):
        """思考增量以 ("...", "thinking") 哨兵元组转发：不混入正文，也不进续传上下文。"""
        llm = FakeLLM([
            ([FakeChunk("", {"reasoning_content": "思考A"}), FakeChunk("答案"), "，B"], RuntimeError("中断")),
            ([], None),
        ])
        chain = _make_chain(llm)

        results = []
        async for chunk, _, _, at in chain._stream_with_retry("P", [], [], "llm_direct", think=False):
            results.append((chunk, at))

        # thinking 与 content 各自独立产出；重试续写上下文只含正文
        assert results == [
            ("思考A", "thinking"),
            ("答案", "llm_direct"),
            ("，B", "llm_direct"),
        ]
        assert "思考A" not in llm.prompts[1]


def _make_full_chain(llm):
    """构造走 llm_direct 全路径的链：决策为不使用知识库。"""
    chain = _make_chain(llm)

    async def _no_kb(*args, **kwargs):
        return False

    chain._should_use_knowledge_base = _no_kb
    return chain


class TestRunStreamConsistency:
    """run() 与 arun_stream() 消费同一 _pipeline 的一致性契约。"""

    async def test_answer_equals_stream_concatenation(self, hermetic):
        """同一链实例上：run() 的完整回答 == arun_stream() 各 chunk 拼接。"""
        script = (["机器学习是", "人工智能的一个分支。"], None)
        llm = FakeLLM([script, script])
        chain = _make_full_chain(llm)

        answer, sources, metadata, answer_type = await chain.run("什么是机器学习？")

        parts = []
        async for chunk, st, sm, at in chain.arun_stream("什么是机器学习？"):
            if at == "reasoning":
                continue
            parts.append((chunk, at))

        assert answer == "".join(p[0] for p in parts) == "机器学习是人工智能的一个分支。"
        assert answer_type == "llm_direct"
        assert all(p[1] == "llm_direct" for p in parts)

    async def test_stream_exposes_reasoning_but_run_ignores_it(self, hermetic):
        """流式路径产出 reasoning 事件（SSE 透明转发），非流式不受影响。"""
        llm = FakeLLM([(["答案"], None), (["答案"], None)])
        chain = _make_full_chain(llm)

        answer, *_ = await chain.run("你好")

        stream_answer_types = []
        async for _, _, _, at in chain.arun_stream("你好"):
            stream_answer_types.append(at)

        assert answer == "答案"
        assert "reasoning" in stream_answer_types
        assert "llm_direct" in stream_answer_types

    async def test_stream_yields_only_4_tuples_no_final_leak(self, hermetic):
        """arun_stream 保持 4 元组 API：final 事件不对外泄漏。"""
        llm = FakeLLM([(["x"], None), (["x"], None)])
        chain = _make_full_chain(llm)

        async for item in chain.arun_stream("问题"):
            assert isinstance(item, tuple) and len(item) == 4
