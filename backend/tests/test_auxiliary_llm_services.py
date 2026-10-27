"""LLM 辅助服务单元测试（SEC-15 覆盖率补测）。

覆盖问题处理、上下文增强、标题生成、文档分析、知识库对比、知识图谱与工具执行器。
全部依赖本地假对象（假 LLM / 假 RAGChain），不触达真实模型与外部服务。
"""

import pytest
from langchain_core.documents import Document

from src.services.context_enhancer import ContextEnhancer
from src.services.document_analyzer import DocumentAnalyzer
from src.services.intent_router import FallbackStrategy, IntentDecision
from src.services.kb_comparator import KBComparator
from src.services.knowledge_graph_generator import KnowledgeGraphGenerator
from src.services.question_processor import QuestionProcessor
from src.services.title_generator import TitleGenerator
from src.services.tool_executor import ToolExecutor


class _Resp:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    """最小 LLM 桩：记录 prompt，按需返回内容或抛错。"""

    def __init__(self, content="", exc=None):
        self.content = content
        self.exc = exc
        self.prompts = []

    async def ainvoke(self, prompt):
        self.prompts.append(prompt)
        if self.exc is not None:
            raise self.exc
        return _Resp(self.content)


# ---------------------------------------------------------------------------
# QuestionProcessor
# ---------------------------------------------------------------------------


async def test_question_rewrite_success_and_json_fallback():
    llm = _FakeLLM('前置说明\n{"rewritten": "重写后的问题", "changes": "补充了背景"}')
    processor = QuestionProcessor(llm)
    result = await processor.rewrite_question("原问题")
    assert result == {
        "rewritten": "重写后的问题",
        "original": "原问题",
        "changes": "补充了背景",
    }

    processor = QuestionProcessor(_FakeLLM("没有 JSON"))
    result = await processor.rewrite_question("原问题")
    assert result["rewritten"] == "原问题"
    assert result["changes"] == "问题无需修改"

    processor = QuestionProcessor(_FakeLLM(exc=RuntimeError("boom")))
    result = await processor.rewrite_question("原问题")
    assert result["changes"] == "重写失败，保持原问题"


async def test_question_classify_success_and_fallbacks():
    content = '{"type": "FACTUAL", "subtype": "定义", "confidence": 0.9, "description": "事实"}'
    result = await QuestionProcessor(_FakeLLM(content)).classify_question("什么是 RAG")
    assert result["type"] == "FACTUAL"
    assert result["subtype"] == "定义"

    result = await QuestionProcessor(_FakeLLM("无结构")).classify_question("问题")
    assert result["type"] == "EXPLORATORY"
    assert result["description"] == "无法确定问题类型"

    result = await QuestionProcessor(_FakeLLM(exc=ValueError("bad"))).classify_question("问题")
    assert result["description"] == "分类失败"


async def test_generate_suggestions_cleans_numbering_and_filters_short():
    content = "1. 什么是向量检索？\n- 如何评估召回率？\n短\n\n没有编号但足够长的问题\n* 最后一条问题"
    result = await QuestionProcessor(_FakeLLM(content)).generate_suggestions(
        question="RAG", history_context="历史", kb_ids=["kb-1", "kb-2"]
    )
    assert result == ["什么是向量检索？", "如何评估召回率？", "没有编号但足够长的问题", "最后一条问题"]


async def test_generate_suggestions_exception_returns_defaults():
    result = await QuestionProcessor(_FakeLLM(exc=RuntimeError("x"))).generate_suggestions()
    assert len(result) == 5
    assert result[0].endswith("？")


# ---------------------------------------------------------------------------
# ContextEnhancer
# ---------------------------------------------------------------------------


def _history(n_turns=2, content="内容"):
    history = []
    for i in range(n_turns):
        history.append({"role": "user", "content": f"{content}-u{i}"})
        history.append({"role": "assistant", "content": f"{content}-a{i}"})
    return history


async def test_context_summary_lru_eviction():
    enhancer = ContextEnhancer(_FakeLLM(), max_history_turns=10, max_summary_length=100)
    enhancer._max_summaries = 2
    enhancer._set_summary("s1", "摘要1")
    enhancer._set_summary("s2", "摘要2")
    enhancer._get_summary("s1")  # 访问 s1，使其成为最近使用
    enhancer._set_summary("s3", "摘要3")
    assert enhancer._get_summary("s1") == "摘要1"
    assert enhancer._get_summary("s2") == ""
    assert enhancer._get_summary("s3") == "摘要3"


async def test_build_history_context_formats_roles_and_prepends_summary():
    enhancer = ContextEnhancer(_FakeLLM(), max_history_turns=4)
    context = await enhancer._build_history_context(_history(3), "s1")
    assert "用户: 内容-u2" in context
    assert "助手: 内容-a2" in context
    assert "内容-u0" not in context, "超出 max_history_turns 的早期轮次应被裁剪"

    enhancer._set_summary("s1", "之前聊过向量库")
    context = await enhancer._build_history_context(_history(1), "s1")
    assert context.startswith("对话摘要: 之前聊过向量库")

    enhancer2 = ContextEnhancer(_FakeLLM())
    enhancer2._set_summary("s9", "只有摘要")
    assert await enhancer2._build_history_context([], "s9") == "只有摘要"


async def test_build_history_context_compresses_when_too_long():
    enhancer = ContextEnhancer(_FakeLLM("压缩后的摘要"), max_history_turns=20)
    long_history = [{"role": "user", "content": "很长的内容" * 200} for _ in range(5)]
    context = await enhancer._build_history_context(long_history, "")
    assert context == "压缩后的摘要"
    assert enhancer.llm.prompts, "超过 2000 字应触发压缩调用"


async def test_compress_context_success_and_failure():
    enhancer = ContextEnhancer(_FakeLLM("摘要"), max_summary_length=5)
    assert await enhancer._compress_context("原始上下文") == "摘要"

    failing = ContextEnhancer(_FakeLLM(exc=RuntimeError("boom")), max_summary_length=5)
    assert await failing._compress_context("原始上下文内容") == "原始上下文内容"[:5]


async def test_resolve_references_paths(monkeypatch):
    enhancer = ContextEnhancer(_FakeLLM("消解后的问题"))
    assert await enhancer._resolve_references("它是什么", []) == "它是什么"
    assert await enhancer._resolve_references("什么是 RAG", _history(1)) == "什么是 RAG"

    # 默认关闭 LLM 消解：含指代词也保持原问题
    assert await enhancer._resolve_references("它是什么", _history(1)) == "它是什么"

    monkeypatch.setattr(
        "src.services.context_enhancer.settings.processing.CONTEXT_RESOLVE_USE_LLM", True
    )
    assert await enhancer._resolve_references("它是什么", _history(1)) == "消解后的问题"

    failing = ContextEnhancer(_FakeLLM(exc=RuntimeError("x")))
    assert await failing._resolve_references("它是什么", _history(1)) == "它是什么"


async def test_update_conversation_summary():
    enhancer = ContextEnhancer(_FakeLLM("新摘要"))
    assert await enhancer._update_conversation_summary([], "s1") == ""

    enhancer._set_summary("s1", "旧摘要")
    assert await enhancer._update_conversation_summary(_history(1), "s1") == "新摘要"
    assert enhancer._get_summary("s1") == "新摘要"

    failing = ContextEnhancer(_FakeLLM(exc=RuntimeError("x")))
    failing._set_summary("s2", "旧摘要")
    assert await failing._update_conversation_summary(_history(1), "s2") == "旧摘要"


async def test_enhance_context_reports_resolution_flag(monkeypatch):
    enhancer = ContextEnhancer(_FakeLLM("它是什么"))
    result = await enhancer.enhance_context("它是什么", _history(1), "s1")
    assert result["has_resolution"] is False
    assert result["resolved_question"] == "它是什么"
    assert "用户: 内容-u0" in result["enhanced_context"]

    monkeypatch.setattr(
        "src.services.context_enhancer.settings.processing.CONTEXT_RESOLVE_USE_LLM", True
    )
    enhancer2 = ContextEnhancer(_FakeLLM("它是什么东西"))
    result = await enhancer2.enhance_context("它是什么", _history(1), "s1")
    assert result["has_resolution"] is True


# ---------------------------------------------------------------------------
# TitleGenerator
# ---------------------------------------------------------------------------


def test_title_default_detection_and_clean():
    generator = TitleGenerator()
    assert generator.is_default_title(None) is True
    assert generator.is_default_title("  ") is True
    assert generator.is_default_title("  new chat ") is True
    assert generator.is_default_title("RAG 检索优化") is False
    assert generator._clean_title("《向量   检索》") == "向量 检索"


async def test_generate_title_fallbacks_and_truncation(monkeypatch):
    generator = TitleGenerator()
    monkeypatch.setattr(
        "src.services.title_generator.settings.title_generation.TITLE_GENERATION_ENABLED", False
    )
    monkeypatch.setattr(
        "src.services.title_generator.settings.title_generation.TITLE_FALLBACK_LENGTH", 4
    )
    assert await generator.generate_title("这是一个很长的问题") == "这是一个"
    assert await generator.generate_title("   ") == "新会话"

    monkeypatch.setattr(
        "src.services.title_generator.settings.title_generation.TITLE_GENERATION_ENABLED", True
    )

    async def _no_llm():
        return None

    monkeypatch.setattr(generator, "_get_llm", _no_llm)
    assert await generator.generate_title("没有模型可用") == "没有模型可用"[:4]

    monkeypatch.setattr(
        "src.services.title_generator.settings.title_generation.TITLE_MAX_LENGTH", 6
    )

    long_llm = _FakeLLM('"超长标题需要被截断"')

    async def _long_llm():
        return long_llm

    monkeypatch.setattr(generator, "_get_llm", _long_llm)
    assert await generator.generate_title("问题") == "超长标题需要"

    blank_llm = _FakeLLM("  ")

    async def _blank_llm():
        return blank_llm

    monkeypatch.setattr(generator, "_get_llm", _blank_llm)
    assert await generator.generate_title("空标题回退") == "空标题回退"[:4]

    failing_llm = _FakeLLM(exc=RuntimeError("x"))

    async def _failing_llm():
        return failing_llm

    monkeypatch.setattr(generator, "_get_llm", _failing_llm)
    assert await generator.generate_title("异常回退") == "异常回退"[:4]


# ---------------------------------------------------------------------------
# DocumentAnalyzer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content,expected_type,expected_topic",
    [
        ("这段代码实现了 API 与算法", "technical", "技术开发"),
        ("财务预算与成本审计", "business", "财务管理"),
        ("调研报告与数据分析", "report", "数据分析"),
        ("使用手册与操作指南", "manual", "使用指南"),
        ("政策规定与通知公告", "policy", "政策法规"),
        ("新闻报道最新动态", "news", "新闻资讯"),
        ("完全无关的内容", "other", "综合内容"),
    ],
)
def test_analyze_document_content_types(content, expected_type, expected_topic):
    doc_type, topics, _ = DocumentAnalyzer.analyze_document_content(content)
    assert doc_type == expected_type
    assert expected_topic in topics


def test_analyze_document_content_domains_and_extra_topics():
    _, topics, domain = DocumentAnalyzer.analyze_document_content("医院药品与疾病诊断")
    assert domain == "healthcare"
    assert "医疗健康" in topics

    _, topics, domain = DocumentAnalyzer.analyze_document_content("学校课程与学生培训")
    assert domain == "education"

    _, topics, domain = DocumentAnalyzer.analyze_document_content("合同诉讼与法律义务")
    assert domain == "legal"

    _, topics, domain = DocumentAnalyzer.analyze_document_content("企业战略与运营管理")
    assert domain == "enterprise"

    _, topics, _ = DocumentAnalyzer.analyze_document_content("安全 数据 系统 流程")
    assert {"安全", "数据分析", "系统设计", "流程管理"}.issubset(set(topics))


def test_evaluate_quality_branches():
    short = DocumentAnalyzer.evaluate_quality("太短")
    assert short["completeness"] == 20
    assert any("增加文档内容" in s for s in short["suggestions"])

    medium = DocumentAnalyzer.evaluate_quality("内容" * 100)
    assert medium["completeness"] == 50

    sentence = "这是一个足够长的句子内容用于测试可读性评分。"
    paragraph = sentence * 10
    long_with_headings = DocumentAnalyzer.evaluate_quality(
        "一、标题\n" + "\n\n".join(paragraph for _ in range(20))
    )
    assert long_with_headings["completeness"] == 90
    assert long_with_headings["readability"] == 85
    assert long_with_headings["structure"] == 90
    assert long_with_headings["suggestions"] == ["文档质量良好，继续保持"]

    long_sentences = DocumentAnalyzer.evaluate_quality(("很长的句子" * 30 + "。") * 3)
    assert long_sentences["readability"] == 50
    assert "句子过长" in long_sentences["readability_comment"]


@pytest.mark.parametrize(
    "score,grade",
    [(95, "优秀"), (85, "良好"), (75, "中等"), (65, "及格"), (30, "需改进")],
)
def test_derive_grade_thresholds(score, grade):
    result = DocumentAnalyzer._derive_grade({"overall_score": score})
    assert result["overall_score"] == score
    assert result["overall_grade"] == grade


def test_derive_grade_handles_string_and_existing_grade():
    result = DocumentAnalyzer._derive_grade({"overall_score": "85.5", "overall_grade": "自定义"})
    assert result["overall_score"] == 85
    assert result["overall_grade"] == "自定义"

    result = DocumentAnalyzer._derive_grade({"overall_score": "not-a-number"})
    assert result["overall_score"] == 0
    assert result["overall_grade"] == "需改进"


class _FakeEncoder:
    """最小 embedding 桩：encode 原样返回文本，便于用 cos_sim 桩控制相似度。"""

    def encode(self, text):
        return text


class _FakeRagChain:
    def __init__(self, docs=None, has_store=True, sentence_transformer="default", error=None,
                 similarity=None, source_texts=None, source_metadata=None):
        self.docs = docs or []
        self._has_store = has_store
        self.sentence_transformer = (
            _FakeEncoder() if sentence_transformer == "default" else sentence_transformer
        )
        self.error = error
        self.similarity = similarity or (lambda a, b: 0.0)
        self.source_texts = source_texts or []
        self.source_metadata = source_metadata or []
        self.similarity_initialized = 0

    def _has_vector_store(self):
        return self._has_store

    def _init_similarity_model(self):
        self.similarity_initialized += 1

    async def _retrieve_documents(self, query, kb_ids=None):
        if self.error is not None:
            raise self.error
        return self.docs

    def _extract_source_info(self, docs):
        return self.source_texts, self.source_metadata

    @property
    def similarity_util(self):
        class _Util:
            def __init__(self, fn):
                self.cos_sim = staticmethod(fn)

        return _Util(self.similarity)


async def test_detect_duplicates_short_circuits():
    analyzer = DocumentAnalyzer(_FakeLLM(), _FakeRagChain(has_store=False))
    assert await analyzer.detect_duplicates("内容") == []

    analyzer = DocumentAnalyzer(_FakeLLM(), _FakeRagChain(sentence_transformer=None))
    assert await analyzer.detect_duplicates("内容") == []


async def test_detect_duplicates_returns_sorted_hits():
    docs = [
        Document(page_content="alpha", metadata={"document_id": "d1", "filename": "a.txt", "kb_id": "k1"}),
        Document(page_content="beta", metadata={"document_id": "d2", "filename": "b.txt", "kb_id": "k1"}),
        Document(page_content="gamma", metadata={"document_id": "d3", "filename": "c.txt", "kb_id": "k1"}),
    ]

    def _cos(a, b):
        return {"alpha": 0.95, "beta": 0.5, "gamma": 0.99}.get(b, 0.0)

    rag_chain = _FakeRagChain(docs=docs, similarity=_cos)
    analyzer = DocumentAnalyzer(_FakeLLM(), rag_chain)
    duplicates = await analyzer.detect_duplicates("query", kb_id="k1", threshold=0.9)
    assert [d["document_id"] for d in duplicates] == ["d3", "d1"]
    assert rag_chain.similarity_initialized == 1

    failing = DocumentAnalyzer(_FakeLLM(), _FakeRagChain(error=RuntimeError("boom")))
    assert await failing.detect_duplicates("query") == []


async def test_analyze_document_success_parse_and_fallback(monkeypatch):
    analyzer = DocumentAnalyzer(_FakeLLM(), _FakeRagChain())
    payload = '{"document_type": "报告", "topics": ["a"], "overall_score": "88.4", "summary": "摘要"}'

    async def _aux():
        return _FakeLLM(payload)

    monkeypatch.setattr(analyzer, "_get_aux_llm", _aux)
    result = await analyzer.analyze_document("文档内容")
    assert result["overall_score"] == 88
    assert result["overall_grade"] == "良好"

    async def _aux_bad():
        return _FakeLLM("没有 JSON")

    monkeypatch.setattr(analyzer, "_get_aux_llm", _aux_bad)
    fallback = await analyzer.analyze_document("文档内容")
    assert fallback["overall_grade"] == "及格"
    assert fallback["summary"] == "无法解析评估结果"

    async def _aux_exc():
        raise RuntimeError("模型不可用")

    monkeypatch.setattr(analyzer, "_get_aux_llm", _aux_exc)
    fallback = await analyzer.analyze_document("文档内容")
    assert fallback["summary"] == "评估失败"


# ---------------------------------------------------------------------------
# KBComparator
# ---------------------------------------------------------------------------


async def test_compare_knowledge_bases_mixed_results():
    docs = [Document(page_content="片段", metadata={"source": "a.txt"})]
    rag_chain = _FakeRagChain(docs=docs, sentence_transformer=None, source_texts=["a.txt"])
    comparator = KBComparator(_FakeLLM("答案"), rag_chain)

    # kb 查询按 kb_id 区分：让第二个知识库抛错、第三个为空
    seen = []

    async def _retrieve(query, kb_ids=None):
        kb = (kb_ids or [""])[0]
        seen.append(kb)
        if kb == "kb-error":
            raise RuntimeError("检索炸了")
        if kb == "kb-empty":
            return []
        return docs

    rag_chain._retrieve_documents = _retrieve

    result = await comparator.compare_knowledge_bases("问题", ["kb-ok", "kb-empty", "kb-error"])
    assert result["kb-ok"]["has_answer"] is True
    assert result["kb-ok"]["answer"] == "答案"
    assert result["kb-empty"]["has_answer"] is False
    assert "检索失败" in result["kb-error"]["answer"]
    assert result["_analysis"]["consistency"] == "high", "只有一条有效答案时一致性为 high"
    assert seen == ["kb-ok", "kb-empty", "kb-error"]


async def test_answer_differences_similarity_paths():
    comparator = KBComparator(_FakeLLM("差异说明"), _FakeRagChain(sentence_transformer=None))
    medium = await comparator._analyze_answer_differences(
        [{"kb_id": "a", "answer": "x"}, {"kb_id": "b", "answer": "y"}], ["a", "b"]
    )
    assert medium["similarity_score"] == 0.5
    assert medium["consistency"] == "medium"

    rag_chain = _FakeRagChain(similarity=lambda a, b: 0.9)
    comparator = KBComparator(_FakeLLM("1. 差异A\n- 差异B"), rag_chain)
    high = await comparator._analyze_answer_differences(
        [{"kb_id": "a", "answer": "x"}, {"kb_id": "b", "answer": "y"}], ["a", "b"],
        kb_name_map={"a": "知识库A"},
    )
    assert high["similarity_score"] == 0.9
    assert high["consistency"] == "high"
    assert high["comparisons"][0]["kb1_name"] == "知识库A"
    assert high["differences"] == ["差异A", "差异B"]

    low = await KBComparator(
        _FakeLLM("差异"), _FakeRagChain(similarity=lambda a, b: 0.2)
    )._analyze_answer_differences(
        [{"kb_id": "a", "answer": "x"}, {"kb_id": "b", "answer": "y"}], ["a", "b"]
    )
    assert low["consistency"] == "low"

    # 差异提取 LLM 失败：仅差异为空，整体一致性仍按相似度判定
    extract_failing = KBComparator(
        _FakeLLM(exc=RuntimeError("boom")), _FakeRagChain(similarity=lambda a, b: 0.9)
    )
    result = await extract_failing._analyze_answer_differences(
        [{"kb_id": "a", "answer": "x"}, {"kb_id": "b", "answer": "y"}], ["a", "b"]
    )
    assert result["consistency"] == "high"
    assert result["differences"] == []

    # 相似度计算本身抛错：外层兜底 unknown
    def _raise_cos(a, b):
        raise RuntimeError("cos 失败")

    outer_failing = KBComparator(_FakeLLM("差异"), _FakeRagChain(similarity=_raise_cos))
    result = await outer_failing._analyze_answer_differences(
        [{"kb_id": "a", "answer": "x"}, {"kb_id": "b", "answer": "y"}], ["a", "b"]
    )
    assert result["consistency"] == "unknown"
    assert result["differences"] == []


def test_get_kb_display_name():
    comparator = KBComparator(_FakeLLM(), _FakeRagChain())
    assert comparator._get_kb_display_name("abcdef123456", None) == "abcdef12"
    assert comparator._get_kb_display_name("abcdef123456", {"abcdef123456": "知识库"}) == "知识库"


# ---------------------------------------------------------------------------
# KnowledgeGraphGenerator
# ---------------------------------------------------------------------------


async def test_knowledge_graph_without_vector_store():
    generator = KnowledgeGraphGenerator(_FakeLLM(), _FakeRagChain(has_store=False))
    result = await generator.generate_knowledge_graph()
    assert result["nodes"] == []
    assert result["summary"] == "没有可用的向量库"


async def test_knowledge_graph_nodes_and_similarity_edges():
    docs = [
        Document(page_content="相同内容", metadata={"document_id": "d1", "kb_id": "kb-1", "filename": "a.txt"}),
        Document(page_content="相同内容", metadata={"document_id": "d2", "kb_id": "kb-1", "filename": "b.txt"}),
        Document(page_content="其他", metadata={"document_id": "d3", "kb_id": "", "filename": "c.txt"}),
    ]
    rag_chain = _FakeRagChain(docs=docs, similarity=lambda a, b: 0.9)
    generator = KnowledgeGraphGenerator(_FakeLLM(), rag_chain)
    result = await generator.generate_knowledge_graph(["kb-1", "kb-2"])

    labels = {(n["type"], n["label"]) for n in result["nodes"]}
    assert ("knowledge_base", "知识库_kb-1") in labels
    assert ("document", "a.txt") in labels
    assert any(e["label"] == "包含" for e in result["edges"])
    assert any(e["label"].startswith("相似(0.90)") for e in result["edges"])

    # 无 kb_ids：直接检索；相似度抛错时跳过该边而不中断
    def _boom(a, b):
        raise RuntimeError("encode 失败")

    rag_chain = _FakeRagChain(docs=docs[:2], similarity=_boom)
    result = await KnowledgeGraphGenerator(_FakeLLM(), rag_chain).generate_knowledge_graph()
    assert len(result["nodes"]) == 3
    assert all(e["label"] == "包含" for e in result["edges"])


async def test_knowledge_graph_failure_returns_empty():
    rag_chain = _FakeRagChain(error=RuntimeError("boom"))
    result = await KnowledgeGraphGenerator(_FakeLLM(), rag_chain).generate_knowledge_graph()
    assert result["nodes"] == []
    assert result["summary"].startswith("生成失败")


# ---------------------------------------------------------------------------
# ToolExecutor
# ---------------------------------------------------------------------------


class _FakeToolManager:
    def __init__(self, results=None, fallback_result=None):
        self.calls = None
        self.execute_calls = []
        self.results = results or []
        self.fallback_result = fallback_result

    async def execute_parallel(self, calls):
        self.calls = calls
        return self.results

    async def execute(self, name, **kwargs):
        self.execute_calls.append((name, kwargs))
        return self.fallback_result


async def test_tool_executor_builds_arguments_per_tool():
    manager = _FakeToolManager(results=[object()])
    executor = ToolExecutor(tool_manager=manager)
    decision = IntentDecision(
        suggested_tools=["weather_query", "calculator", "get_current_time", "web_search", "custom_tool"]
    )
    await executor.execute(decision, "问题", "历史")

    args_by_tool = {c["tool_name"]: c["arguments"] for c in manager.calls}
    assert args_by_tool["weather_query"] == {"question": "问题"}
    assert args_by_tool["calculator"] == {"expression": "问题"}
    assert args_by_tool["get_current_time"] == {}
    assert args_by_tool["web_search"] == {"query": "问题", "reason": "回答用户问题"}
    assert args_by_tool["custom_tool"] == {"question": "问题", "history_context": "历史"}


async def test_tool_executor_empty_and_fallback():
    executor = ToolExecutor(tool_manager=_FakeToolManager())
    assert await executor.execute(IntentDecision(), "问题") == []

    from src.services.tools.tool_manager import ToolResult

    ok_result = ToolResult(tool_name="web_search", output="ok", success=True)
    manager = _FakeToolManager(
        results=[ToolResult(tool_name="weather_query", success=False, error="失败")],
        fallback_result=ok_result,
    )
    executor = ToolExecutor(tool_manager=manager)
    decision = IntentDecision(
        suggested_tools=["weather_query"], fallback_strategy=FallbackStrategy.WEB_SEARCH
    )
    results = await executor.execute_with_fallback(decision, "问题")
    assert results == [ok_result]
    assert manager.execute_calls[0][0] == "web_search"

    manager2 = _FakeToolManager(results=[ok_result])
    executor2 = ToolExecutor(tool_manager=manager2)
    results2 = await executor2.execute_with_fallback(
        IntentDecision(suggested_tools=["web_search"], fallback_strategy=FallbackStrategy.WEB_SEARCH),
        "问题",
    )
    assert results2 == [ok_result]
    assert manager2.execute_calls == []
