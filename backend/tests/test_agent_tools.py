"""Agent 演进 Phase 1 单测。

覆盖：
- KBRetrievalService（检索下沉服务的口径与回退）
- kb_search / wiki_lookup 工具（格式化、无 KB 引导、异常兜底）
- MilvusService._build_filter_expr 的 source_kind 过滤与注入防御
- FunctionCallingHandler native FC 决策与自动回退
- ToolMetaRegistry.ensure_defaults 自动补缺省注册
"""

import asyncio
from types import SimpleNamespace

import pytest

from src.services.kb_retrieval_service import KBRetrievalService
from src.services.milvus_service import MilvusService
from src.services.search_agent import (
    FunctionCallingHandler,
    SearchToolkit,
    Tool,
    ToolCall,
)


# ----------------------------------------------------------------------
# 测试脚手架
# ----------------------------------------------------------------------
class _FakeVectorStore:
    """记录调用参数的假向量存储。"""

    def __init__(self, docs=None, hybrid_error=False):
        self.docs = docs or []
        self.hybrid_error = hybrid_error
        self.calls = []

    async def search_hybrid(self, query, **kwargs):
        self.calls.append(("hybrid", query, kwargs))
        if self.hybrid_error:
            raise RuntimeError("hybrid down")
        return list(self.docs)

    async def search_hybrid_multi(self, queries, **kwargs):
        self.calls.append(("hybrid_multi", queries, kwargs))
        return list(self.docs)

    async def search_dense(self, query, **kwargs):
        self.calls.append(("dense", query, kwargs))
        return list(self.docs)


def _doc(content="内容", source="a.md", kind="raw", score=0.9):
    from langchain_core.documents import Document

    return Document(
        page_content=content,
        metadata={
            "source": source,
            "heading_path": "H1/H2",
            "chunk_index": 0,
            "source_kind": kind,
            "document_id": "d1",
            "score": score,
        },
    )


# ----------------------------------------------------------------------
# KBRetrievalService
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_kb_retrieval_service_hybrid_passthrough(monkeypatch):
    monkeypatch.setattr(
        "src.services.kb_retrieval_service.settings.processing.KB_ENABLE_HYBRID_SEARCH",
        True,
    )
    fake = _FakeVectorStore(docs=[_doc()])
    svc = KBRetrievalService(vector_store=fake)

    docs = await svc.retrieve("问题", kb_ids=["kb1"], source_kind="wiki")

    assert len(docs) == 1
    kind, query, kwargs = fake.calls[0]
    assert kind == "hybrid"
    assert kwargs["kb_ids"] == ["kb1"]
    assert kwargs["source_kind"] == "wiki"


@pytest.mark.asyncio
async def test_kb_retrieval_service_fallback_to_dense(monkeypatch):
    monkeypatch.setattr(
        "src.services.kb_retrieval_service.settings.processing.KB_ENABLE_HYBRID_SEARCH",
        True,
    )
    fake = _FakeVectorStore(docs=[_doc()], hybrid_error=True)
    svc = KBRetrievalService(vector_store=fake)

    docs = await svc.retrieve("问题", kb_ids=["kb1"])

    assert len(docs) == 1
    assert fake.calls[-1][0] == "dense"


@pytest.mark.asyncio
async def test_kb_retrieval_service_no_store_returns_empty():
    svc = KBRetrievalService(vector_store=None)

    async def _fail():
        raise RuntimeError("no milvus")

    # 懒加载失败时返回空列表而非抛异常
    async def _get_none():
        return None

    async def _ensure():
        return None

    svc._ensure_vector_store = _ensure
    docs = await svc.retrieve("问题")
    assert docs == []


# ----------------------------------------------------------------------
# kb_search / wiki_lookup 工具
# ----------------------------------------------------------------------
def _make_tool(cls, docs, **tool_kwargs):
    svc = KBRetrievalService(vector_store=_FakeVectorStore(docs=docs))

    async def _ensure():
        return svc

    tool = cls(kb_retrieval_service=svc, **tool_kwargs)
    tool._ensure_service = _ensure
    return tool


@pytest.mark.asyncio
async def test_kb_search_tool_formats_results():
    tool = _make_tool(
        __import__(
            "src.services.tools.plugins.kb_search_tool", fromlist=["KBSearchTool"]
        ).KBSearchTool,
        [_doc("片段内容")],
        kb_ids=["kb1"],
    )
    result = await tool.execute(query="查询")

    assert result.success
    assert "[1]" in result.output and "片段内容" in result.output
    assert result.sources[0]["source_kind"] == "raw"
    assert result.sources[0]["document_id"] == "d1"


@pytest.mark.asyncio
async def test_wiki_lookup_tool_filters_wiki(monkeypatch):
    from src.services.tools.plugins.wiki_lookup_tool import WikiLookupTool

    captured = {}

    class _CaptureStore(_FakeVectorStore):
        async def search_hybrid(self, query, **kwargs):
            captured.update(kwargs)
            return [_doc("wiki 页", kind="wiki")]

    svc = KBRetrievalService(vector_store=_CaptureStore())

    async def _ensure():
        return svc

    tool = WikiLookupTool(kb_retrieval_service=svc, kb_ids=["kb1"])
    tool._ensure_service = _ensure
    result = await tool.execute(query="主题")

    assert result.success
    assert captured.get("source_kind") == "wiki"
    assert result.sources[0]["source_kind"] == "wiki"


@pytest.mark.asyncio
async def test_kb_search_tool_without_kb_returns_guidance():
    from src.services.tools.plugins.kb_search_tool import KBSearchTool

    tool = KBSearchTool(kb_retrieval_service=KBRetrievalService())
    result = await tool.execute(query="查询")

    assert result.success
    assert "未选择知识库" in result.output


@pytest.mark.asyncio
async def test_kb_search_tool_empty_query_fails():
    from src.services.tools.plugins.kb_search_tool import KBSearchTool

    tool = KBSearchTool(kb_retrieval_service=KBRetrievalService(), kb_ids=["kb1"])
    result = await tool.execute(query="")

    assert not result.success


# ----------------------------------------------------------------------
# Milvus filter 表达式：source_kind 过滤与注入防御
# ----------------------------------------------------------------------
def test_build_filter_expr_with_source_kind():
    expr = MilvusService._build_filter_expr(kb_ids=["00000000-0000-0000-0000-000000000001"], source_kind="wiki")
    assert 'source_kind == "wiki"' in expr
    assert "kb_id in" in expr


def test_build_filter_expr_rejects_injection():
    with pytest.raises(ValueError):
        MilvusService._build_filter_expr(source_kind='wiki" || kb_id != "')


def test_build_filter_expr_no_conditions():
    assert MilvusService._build_filter_expr() is None


# ----------------------------------------------------------------------
# FunctionCallingHandler：native FC 决策与回退
# ----------------------------------------------------------------------
def _toolkit_stub():
    async def _handler(**kwargs):
        return "ok"

    web = Tool(
        name="web_search",
        description="联网搜索",
        parameters={"type": "object", "properties": {"query": {"type": "string"}}},
        handler=_handler,
    )
    import json as _json

    return SimpleNamespace(
        tools=[web],
        tools_prompt=lambda: _json.dumps(
            [{"name": web.name, "description": web.description, "parameters": web.parameters}],
            ensure_ascii=False,
        ),
    )


class _NativeLLM:
    """模拟支持原生 FC 的 LLM。"""

    def __init__(self, tool_calls=None, content=""):
        self._tool_calls = tool_calls or []
        self._content = content
        self.bound_schemas = None

    def bind_tools(self, tools):
        self.bound_schemas = tools
        return self

    async def ainvoke(self, messages):
        return SimpleNamespace(tool_calls=self._tool_calls, content=self._content)


class _BrokenNativeLLM(_NativeLLM):
    """模拟 bind_tools 抛异常（旧 Ollama 不支持 tools）的 LLM。"""

    def bind_tools(self, tools):
        raise TypeError("server does not support tools")

    async def ainvoke(self, prompt):
        return SimpleNamespace(
            content='<tool_call>[{"name": "web_search", "arguments": {"query": "天气"}}]</tool_call>'
        )


@pytest.mark.asyncio
async def test_native_fc_returns_tool_calls(monkeypatch):
    monkeypatch.setattr(
        "src.services.search_agent.settings.search.SEARCH_AGENT_NATIVE_FC", True
    )
    llm = _NativeLLM(
        tool_calls=[{"name": "web_search", "args": {"query": "北京天气"}, "id": "1"}]
    )
    handler = FunctionCallingHandler(llm, _toolkit_stub())

    tool_calls, content = await handler._decide("北京天气", "")

    assert len(tool_calls) == 1
    assert tool_calls[0].name == "web_search"
    assert tool_calls[0].arguments == {"query": "北京天气"}
    assert content == ""
    # schema 传给了 bind_tools（OpenAI function 格式）
    assert llm.bound_schemas[0]["type"] == "function"


@pytest.mark.asyncio
async def test_native_fc_direct_answer(monkeypatch):
    monkeypatch.setattr(
        "src.services.search_agent.settings.search.SEARCH_AGENT_NATIVE_FC", True
    )
    llm = _NativeLLM(tool_calls=[], content="机器学习是……")
    handler = FunctionCallingHandler(llm, _toolkit_stub())

    tool_calls, content = await handler._decide("什么是机器学习", "")

    assert tool_calls == []
    assert content == "机器学习是……"


@pytest.mark.asyncio
async def test_native_fc_failure_falls_back_to_prompt(monkeypatch):
    monkeypatch.setattr(
        "src.services.search_agent.settings.search.SEARCH_AGENT_NATIVE_FC", True
    )
    llm = _BrokenNativeLLM()
    handler = FunctionCallingHandler(llm, _toolkit_stub())

    tool_calls, content = await handler._decide("吕梁天气", "")

    assert len(tool_calls) == 1
    assert tool_calls[0].name == "web_search"
    assert tool_calls[0].arguments.get("query") == "天气"


@pytest.mark.asyncio
async def test_native_fc_disabled_uses_prompt_path(monkeypatch):
    monkeypatch.setattr(
        "src.services.search_agent.settings.search.SEARCH_AGENT_NATIVE_FC", False
    )
    llm = _BrokenNativeLLM()
    handler = FunctionCallingHandler(llm, _toolkit_stub())

    tool_calls, _ = await handler._decide("吕梁天气", "")

    assert len(tool_calls) == 1


@pytest.mark.asyncio
async def test_search_toolkit_whitelist_view():
    """SearchToolkit 基于全局 ToolManager 的白名单视图：仅暴露白名单工具。"""
    toolkit = SearchToolkit(web_search_service=SimpleNamespace())
    names = {t.name for t in toolkit.tools}

    assert names <= set(SearchToolkit.DEFAULT_TOOL_NAMES)
    assert "web_search" in names
    # 全局注册表包含全部插件（视图外工具仍可通过 tool_manager 执行）
    assert toolkit.tool_manager.registry.get("calculator") is not None


def test_search_toolkit_reuses_global_tool_manager():
    """L1-b 锁定：SearchToolkit 复用全局 get_tool_manager() 单例，无独立注册表。"""
    from src.services.tools.tool_manager import get_tool_manager

    toolkit = SearchToolkit(web_search_service=SimpleNamespace())
    # 同一 ToolManager 实例（注册中心唯一，发现/同步只发生一次）
    assert toolkit.tool_manager is get_tool_manager()


# ----------------------------------------------------------------------
# ToolMetaRegistry.ensure_defaults
# ----------------------------------------------------------------------
def test_tool_meta_ensure_defaults():
    from src.services.intent_router.tool_registry import (
        ToolMetaRegistry,
        get_tool_meta_registry,
    )

    registry = get_tool_meta_registry()
    before = registry.get("kb_search") is not None

    registry.ensure_defaults(
        {
            "kb_search": "已有描述（不应覆盖）",
            "_future_tool": "未来工具描述",
        }
    )

    meta = registry.get("_future_tool")
    assert meta is not None
    assert meta.trigger_patterns == []
    assert meta.conflict_priority == 0
    if before:
        # 已登记的工具不被覆盖
        assert registry.get("kb_search").description != "已有描述（不应覆盖）"


# ----------------------------------------------------------------------
# AgentOrchestrator：有界循环
# ----------------------------------------------------------------------
from src.services.agent_orchestrator import AgentOrchestrator, AgentLoopState
from src.services.tools.tool_manager import BaseTool, ToolResult as MgrToolResult


class _EchoTool(BaseTool):
    """记录调用参数并返回固定输出的假工具。"""

    name = "web_search"
    description = "联网搜索"
    parameters = {"type": "object", "properties": {"query": {"type": "string"}}}
    calls: list = []

    async def execute(self, **kwargs) -> MgrToolResult:
        _EchoTool.calls.append(kwargs)
        return MgrToolResult(
            tool_name=self.name,
            input_arguments=kwargs,
            output=f"搜索结果（query={kwargs.get('query', '')}）",
            sources=[{"url": "http://example.com/x", "title": "X", "page_content": "内容"}],
        )


class _KBTool(BaseTool):
    name = "kb_search"
    description = "知识库检索"
    parameters = {"type": "object", "properties": {"query": {"type": "string"}}}
    calls: list = []

    async def execute(self, **kwargs) -> MgrToolResult:
        _KBTool.calls.append(kwargs)
        return MgrToolResult(
            tool_name=self.name,
            input_arguments=kwargs,
            output=f"KB片段（kb_ids={kwargs.get('kb_ids')}）",
        )


class _FakeRegistry:
    def __init__(self, tools):
        self._tools = {t.name: t for t in tools}

    def get(self, name):
        return self._tools.get(name)


class _FakeToolManager:
    def __init__(self, tools):
        self.registry = _FakeRegistry(tools)

    async def execute_parallel(self, calls):
        results = []
        for c in calls:
            tool = self.registry.get(c["tool_name"])
            if tool is None:
                results.append(MgrToolResult(tool_name=c["tool_name"], success=False, error="未注册"))
            else:
                results.append(await tool.execute(**c["arguments"]))
        return results


class _ScriptedLLM:
    """按脚本顺序返回决策结果的假 LLM（native FC 形态）。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        self.prompts.append(messages[0].content if messages else "")
        return self.responses.pop(0)


class _FakeAnswerGenerator:
    def __init__(self, chunks=("综合答案：", "依据[1]…")):
        self.chunks = list(chunks)
        self.last_kwargs = None

    async def generate_stream(self, **kwargs):
        self.last_kwargs = kwargs
        for c in self.chunks:
            yield c, None, None


def _ns(tool_calls=None, content=""):
    return SimpleNamespace(tool_calls=tool_calls or [], content=content)


async def _collect(agen):
    return [ev async for ev in agen]


@pytest.mark.asyncio
async def test_orchestrator_final_answer_after_tool(monkeypatch):
    """工具→最终答案 两步循环（纯 Agent 模式）。"""
    monkeypatch.setattr(
        "src.services.search_agent.settings.search.AGENT_TOOLS_ENABLED", "web_search"
    )
    _EchoTool.calls = []
    llm = _ScriptedLLM([
        _ns(tool_calls=[{"name": "web_search", "args": {"query": "q1"}}]),
        _ns(content="最终答案[1]"),
    ])
    orch = AgentOrchestrator(
        llm=llm,
        tool_manager=_FakeToolManager([_EchoTool()]),
        answer_generator=_FakeAnswerGenerator(),
    )

    events = await _collect(orch.run_stream("问题"))

    kinds = [e[0] for e in events]
    assert "reasoning" in kinds and "chunk" in kinds
    result = events[-1][1]
    assert result["reason"] == "final_answer"
    assert result["steps"] == 2
    assert len(_EchoTool.calls) == 1
    # 决策 prompt 包含观察结果（scratchpad 生效）
    assert "搜索结果" in llm.prompts[1]


@pytest.mark.asyncio
async def test_orchestrator_dedupes_same_call(monkeypatch):
    """同参数重复调用被拦截为占位观察。"""
    monkeypatch.setattr(
        "src.services.search_agent.settings.search.AGENT_TOOLS_ENABLED", "web_search"
    )
    _EchoTool.calls = []
    llm = _ScriptedLLM([
        _ns(tool_calls=[{"name": "web_search", "args": {"query": "q1"}}]),
        _ns(tool_calls=[{"name": "web_search", "args": {"query": "q1"}}]),
        _ns(content="答案"),
    ])
    orch = AgentOrchestrator(
        llm=llm,
        tool_manager=_FakeToolManager([_EchoTool()]),
    )

    events = await _collect(orch.run_stream("问题"))

    assert len(_EchoTool.calls) == 1  # 第二次同参数调用被去重
    result = events[-1][1]
    assert result["reason"] == "final_answer"


@pytest.mark.asyncio
async def test_orchestrator_max_steps_then_synthesize(monkeypatch):
    """步数耗尽后进入工具结果综合。"""
    monkeypatch.setattr(
        "src.services.search_agent.settings.search.AGENT_TOOLS_ENABLED", "web_search"
    )
    monkeypatch.setattr("src.services.search_agent.settings.search.SEARCH_REACT_MAX_STEPS", 2)
    llm = _ScriptedLLM([
        _ns(tool_calls=[{"name": "web_search", "args": {"query": "q1"}}]),
        _ns(tool_calls=[{"name": "web_search", "args": {"query": "q2"}}]),
    ])
    generator = _FakeAnswerGenerator()
    orch = AgentOrchestrator(
        llm=llm,
        tool_manager=_FakeToolManager([_EchoTool()]),
        answer_generator=generator,
    )

    events = await _collect(orch.run_stream("问题"))

    result = events[-1][1]
    assert result["reason"] == "max_steps"
    assert result["answer"] == "综合答案：依据[1]…"
    assert generator.last_kwargs["tool_results"]


@pytest.mark.asyncio
async def test_orchestrator_time_budget(monkeypatch):
    """时间预算耗尽（0ms）时不执行任何工具，直接空结果。"""
    monkeypatch.setattr(
        "src.services.search_agent.settings.search.AGENT_TOOLS_ENABLED", "web_search"
    )
    monkeypatch.setattr("src.services.search_agent.settings.search.AGENT_TIME_BUDGET_MS", 0)
    llm = _ScriptedLLM([_ns(tool_calls=[{"name": "web_search", "args": {"query": "q1"}}])])
    _EchoTool.calls = []
    orch = AgentOrchestrator(
        llm=llm,
        tool_manager=_FakeToolManager([_EchoTool()]),
        answer_generator=_FakeAnswerGenerator(),
    )

    events = await _collect(orch.run_stream("问题"))

    result = events[-1][1]
    assert result["reason"] == "time_budget"
    assert result["answer"] == ""
    assert _EchoTool.calls == []


@pytest.mark.asyncio
async def test_orchestrator_injects_kb_ids(monkeypatch):
    """kb_search 调用自动注入会话 kb_ids。"""
    monkeypatch.setattr(
        "src.services.search_agent.settings.search.AGENT_TOOLS_ENABLED", "kb_search"
    )
    kb_tool = _KBTool()
    _KBTool.calls = []
    llm = _ScriptedLLM([
        _ns(tool_calls=[{"name": "kb_search", "args": {"query": "q1"}}]),
        _ns(content="KB答案"),
    ])
    orch = AgentOrchestrator(
        llm=llm,
        tool_manager=_FakeToolManager([kb_tool]),
    )

    events = await _collect(orch.run_stream("问题", kb_ids=["kb-1", "kb-2"]))

    # 工具实收参数包含注入的 kb_ids（模型无法得知会话 KB ID，由编排器注入）
    assert _KBTool.calls == [{"query": "q1", "kb_ids": ["kb-1", "kb-2"]}]
    # 第二步决策 prompt 的 scratchpad 观察中体现注入结果
    assert "['kb-1', 'kb-2']" in llm.prompts[1]
    # 模型直接答案作为 chunk 流出（纯 Agent 模式 direct answer 路径）
    chunks = [e for e in events if e[0] == "chunk"]
    assert chunks and chunks[0][1] == "KB答案"


@pytest.mark.asyncio
async def test_orchestrator_collect_only_mode(monkeypatch):
    """混合模式：仅收集上下文，产出 result.context，不流式 chunk。"""
    monkeypatch.setattr(
        "src.services.search_agent.settings.search.AGENT_TOOLS_ENABLED", "web_search"
    )
    llm = _ScriptedLLM([
        _ns(tool_calls=[{"name": "web_search", "args": {"query": "q1"}}]),
        _ns(content=""),
    ])
    orch = AgentOrchestrator(
        llm=llm,
        tool_manager=_FakeToolManager([_EchoTool()]),
    )

    events = await _collect(orch.run_stream("问题", collect_only=True))

    kinds = [e[0] for e in events]
    assert "chunk" not in kinds
    result = events[-1][1]
    assert "搜索结果" in result["context"]
    assert result["sources"][0]["url"] == "http://example.com/x"


@pytest.mark.asyncio
async def test_orchestrator_decide_error_ends_loop(monkeypatch):
    """决策异常安全结束循环，返回空答案（触发上层降级）。"""
    monkeypatch.setattr(
        "src.services.search_agent.settings.search.AGENT_TOOLS_ENABLED", "web_search"
    )

    class _BrokenLLM:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            raise RuntimeError("llm down")

    orch = AgentOrchestrator(
        llm=_BrokenLLM(),
        tool_manager=_FakeToolManager([_EchoTool()]),
    )

    events = await _collect(orch.run_stream("问题"))

    result = events[-1][1]
    assert result["reason"] == "decide_error"
    assert result["answer"] == ""


# ---------------------------------------------------------------------------
# L1-c 自省强化：连续失败时引导换策略或基于自身知识回答
# ---------------------------------------------------------------------------
def _loop_with_obs(observations):
    import time
    from src.config import settings

    loop = AgentLoopState(deadline=time.perf_counter() + 60)
    for i, obs in enumerate(observations, start=1):
        loop.scratchpad.append({"step": i, "tool": "web_search", "args": {"query": f"q{i}"}, "observation": obs})
    return loop


def test_consecutive_failed_steps_counts_only_trailing_failures():
    """连续失败计数仅统计 scratchpad 尾部连续失败步，遇有效结果即停。"""
    orch = AgentOrchestrator(llm=_ScriptedLLM([]), tool_manager=_FakeToolManager([_EchoTool()]))
    # 前 2 步有效，后 2 步连续失败
    loop = _loop_with_obs(["有效结果", "有效结果", "执行失败: timeout", "重复调用（相同参数已执行过）。"])
    assert orch._consecutive_failed_steps(loop) == 2
    # 全失败
    assert orch._consecutive_failed_steps(_loop_with_obs(["执行失败: 404"] * 3)) == 3
    # 全有效 / 空 scratchpad
    assert orch._consecutive_failed_steps(_loop_with_obs(["ok", "ok"])) == 0
    assert orch._consecutive_failed_steps(AgentLoopState(deadline=1.0)) == 0


def test_build_decide_prompt_injects_reflect_on_consecutive_failures():
    """连续失败 ≥2 时，prompt 追加『基于自身知识回答』自省引导。"""
    orch = AgentOrchestrator(llm=_ScriptedLLM([]), tool_manager=_FakeToolManager([_EchoTool()]))
    loop = _loop_with_obs(["执行失败: err", "执行失败: err"])
    prompt = orch._build_decide_prompt("问题", "", loop, kb_ids=None, memory_context="")
    assert "已连续 2 步未能获得有效结果" in prompt
    assert "知识库未覆盖，以下基于我方常识回答" in prompt


def test_build_decide_prompt_no_reflect_without_failures():
    """无连续失败时不注入自省分支，保持基线提示。"""
    orch = AgentOrchestrator(llm=_ScriptedLLM([]), tool_manager=_FakeToolManager([_EchoTool()]))
    loop = _loop_with_obs(["有效观察"])
    prompt = orch._build_decide_prompt("问题", "", loop, kb_ids=None, memory_context="")
    assert "知识库未覆盖" not in prompt
    assert "已连续" not in prompt


# ---------------------------------------------------------------------------
# L2 分层规划
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_plan_step_parses_steps(monkeypatch):
    """_plan_step 解析『步骤N：目标』为 ≤3 步提纲，显式无需拆解/异常则空。"""
    from src.config import settings

    monkeypatch.setattr(settings.search, "AGENT_PLAN_ENABLED", True)
    orch = AgentOrchestrator(llm=_ScriptedLLM([]), tool_manager=_FakeToolManager([_EchoTool()]))
    loop = AgentLoopState(deadline=1.0)

    orch.llm.responses = [_ns(content="步骤1：查知识库\n步骤2：查网页\n步骤3：综合\n步骤4：多余")]
    assert await orch._plan_step("问题", "", loop) == ["查知识库", "查网页", "综合"]

    orch.llm.responses = [_ns(content="无需拆解。")]
    assert await orch._plan_step("问题", "", loop) == []

    orch.llm.responses = [_ns(content="乱输出")]
    assert await orch._plan_step("问题", "", loop) == []

    orch.llm.responses = [_ns(content="")]  # 空响应
    assert await orch._plan_step("问题", "", loop) == []


def test_build_decide_prompt_injects_plan(monkeypatch):
    """plan_dirty 时 prompt 注入剩余计划 + 已完成计划；未规划时保持基线。"""
    orch = AgentOrchestrator(llm=_ScriptedLLM([]), tool_manager=_FakeToolManager([_EchoTool()]))

    loop = AgentLoopState(deadline=1.0)
    loop.plan = ["查知识库", "查网页"]
    loop.plan_done = ["查知识库"]
    loop.plan_dirty = True
    prompt = orch._build_decide_prompt("问题", "", loop, kb_ids=None, memory_context="")
    assert "执行计划（剩余）：[1]查知识库、[2]查网页" in prompt
    assert "已完成计划：查知识库" in prompt

    # 未规划：plan 块不注入
    loop2 = AgentLoopState(deadline=1.0)
    prompt2 = orch._build_decide_prompt("问题", "", loop2, kb_ids=None, memory_context="")
    assert "执行计划" not in prompt2


@pytest.mark.asyncio
async def test_run_stream_emits_plan_event_when_enabled(monkeypatch):
    """AGENT_PLAN_ENABLED=true 时首步产 plan 并发射 planned reason 事件（纯 Agent 模式）。"""
    import json as _json

    from src.config import settings

    monkeypatch.setattr(settings.search, "AGENT_PLAN_ENABLED", True)
    monkeypatch.setattr(settings.search, "SEARCH_REACT_MAX_STEPS", 3)
    main_llm = _ScriptedLLM(
        [
            _ns(content="步骤1：查知识库"),       # 规划响应
            _ns(content=""),                       # 决策1：无工具 → final answer 空（综合兜底）
        ]
    )
    orch = AgentOrchestrator(
        llm=main_llm,
        tool_manager=_FakeToolManager([_EchoTool()]),
        answer_generator=_FakeAnswerGenerator(),
    )
    events = await _collect(orch.run_stream("问题", collect_only=False))
    types = [e[0] for e in events]
    # 首个 reasoning 为 planned（携带 plan）
    assert types[0] == "reasoning"
    planned = _json.loads(events[0][1])
    assert planned["step"] == "agent_loop"
    assert planned["status"] == "planned"
    assert planned["metadata"]["plan"] == ["查知识库"]
