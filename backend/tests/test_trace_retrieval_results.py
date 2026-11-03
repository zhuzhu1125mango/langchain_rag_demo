"""C11：检索结果必须落 trace。

此前 `TraceCollector.set_kb_results` / `set_search_results` 定义了却**从未被调用**，
93 条 trace 的 `kb_results`、`search_results` 全是 null。后果不是"少个字段"，
而是出问题时无法区分两种完全不同的故障：

  - 没检索到     → 要改召回（索引、切分、embedding）
  - 检索到但没用 → 要改重排或生成（分数阈值、prompt、上下文拼接）

两者修法相反，而 trace 里看到的都是"空"，只能靠猜。这组测试锁住"写得进去"。
"""

from langchain_core.documents import Document

from src.services.trace_collector import TraceCollector


class TestKbResults:
    def test_serialises_documents_with_scores(self):
        """rerank_score 必须留下来——它是判断「检索到但被判不相关」的唯一证据。"""
        trace = TraceCollector()
        docs = [
            Document(
                page_content="生产环境部署步骤……",
                metadata={"filename": "deployment.md", "rerank_score": 0.82, "chunk_index": 7},
            ),
            Document(
                page_content="无关内容",
                metadata={"filename": "other.md", "rerank_score": 0.11},
            ),
        ]

        trace.set_kb_results(docs)

        rows = trace.data["kb_results"]
        assert len(rows) == 2
        assert rows[0]["metadata"]["rerank_score"] == 0.82
        assert rows[0]["metadata"]["filename"] == "deployment.md"
        # 分数低的也要留：正是它说明「检索到了但不相关」
        assert rows[1]["metadata"]["rerank_score"] == 0.11

    def test_truncates_long_content(self):
        """正文截断到 500 字：trace 是诊断用，不是存档用，避免把整篇文档塞进库。"""
        trace = TraceCollector()
        trace.set_kb_results([Document(page_content="x" * 2000, metadata={})])

        content = trace.data["kb_results"][0]["page_content"]
        assert len(content) == 500

    def test_empty_results_are_recorded_as_empty_list(self):
        """`[]` 与 `null` 语义不同：前者是「检索过但没命中」，后者是「没记录」。"""
        trace = TraceCollector()
        trace.set_kb_results([])

        assert trace.data["kb_results"] == []
        assert trace.data["kb_results"] is not None

    def test_accepts_plain_dicts(self):
        trace = TraceCollector()
        trace.set_kb_results([{"page_content": "x", "metadata": {"a": 1}}])

        assert trace.data["kb_results"][0]["metadata"] == {"a": 1}


class TestSearchResults:
    def test_accepts_dict_sources(self):
        """web_sources_for_citation 是 List[Dict]，直接能序列化。"""
        trace = TraceCollector()
        trace.set_search_results([{"title": "北京天气", "url": "https://example.com", "snippet": "晴"}])

        assert trace.data["search_results"][0]["url"] == "https://example.com"

    def test_accepts_documents(self):
        trace = TraceCollector()
        trace.set_search_results([Document(page_content="网页正文", metadata={"url": "https://a.b"})])

        assert trace.data["search_results"][0]["metadata"]["url"] == "https://a.b"


class TestBothSurviveColumnFilter:
    """两个字段都是 RequestTrace 的真实列，不能被未知列过滤掉（C7 的回归守卫）。"""

    def test_payload_keeps_retrieval_columns(self):
        from src.models.request_trace import RequestTrace

        trace = TraceCollector()
        trace.set_basic(question="q", session_id="s", user_id="u")
        trace.set_kb_results([Document(page_content="kb", metadata={"rerank_score": 0.5})])
        trace.set_search_results([{"title": "t", "url": "u"}])

        payload = trace._to_record_payload()

        assert payload["kb_results"] == [{"page_content": "kb", "metadata": {"rerank_score": 0.5}}]
        assert payload["search_results"][0]["title"] == "t"
        RequestTrace(**payload)  # 过滤后仍可安全构造
