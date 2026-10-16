"""Wiki 导航器与导航式 wiki_lookup 工具的单测（全离线，mock 全部外部依赖）。

覆盖阶段一 D1：入口检索合并双 source_kind、Related 链接附加与格式化、
按页取读（拼接/截断/未命中）、工具两模式与参数校验。
"""

from types import SimpleNamespace

import pytest

import src.services.wiki_navigator as nav_module
from src.services.tools.plugins.wiki_lookup_tool import WikiLookupTool
from src.services.wiki_navigator import WikiNavigator, format_related


class FakeDoc:
    def __init__(self, content, metadata):
        self.page_content = content
        self.metadata = dict(metadata)


class FakeRetrievalService:
    """按 source_kind 返回预置切片，记录调用参数。"""

    def __init__(self, by_kind):
        self.by_kind = by_kind
        self.calls = []

    async def retrieve(self, query, kb_ids=None, source_kind=None, **kwargs):
        self.calls.append({"source_kind": source_kind, "kb_ids": kb_ids})
        return list(self.by_kind.get(source_kind, []))


class FakeVectorStore:
    def __init__(self, chunks_by_doc):
        self.chunks_by_doc = chunks_by_doc

    async def get_chunks_by_document_id(self, doc_id):
        return list(self.chunks_by_doc.get(doc_id, []))


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
        return FakeResult(FAKE_PAGES)


class FakeSessionMaker:
    def __call__(self):
        return self

    async def __aenter__(self):
        return FakeDB()

    async def __aexit__(self, *_a):
        return False


FAKE_PAGES = [
    SimpleNamespace(
        id="page-1",
        kb_id="kb-1",
        title="考勤管理",
        page_type="entity",
        links=["远程办公", "请假制度"],
        status="active",
    ),
    SimpleNamespace(
        id="page-2",
        kb_id="kb-1",
        title="远程办公",
        page_type="topic",
        links=["考勤管理"],
        status="active",
    ),
]


@pytest.fixture
def patch_db(monkeypatch):
    monkeypatch.setattr(nav_module, "_db_session_maker", FakeSessionMaker(), raising=False)
    monkeypatch.setattr(
        "src.database.async_session_maker", FakeSessionMaker()
    )


async def test_search_entries_merges_both_kinds_and_attaches_links(patch_db):
    """入口检索合并 wiki 与 wiki_syn 两路，并附加命中页 links。"""
    service = FakeRetrievalService(
        {
            "wiki": [FakeDoc("考勤页内容", {"document_id": "page-1", "source_kind": "wiki"})],
            "wiki_syn": [
                FakeDoc(
                    "综合页内容",
                    {"document_id": "page-2", "source_kind": "wiki_syn"},
                )
            ],
        }
    )
    navigator = WikiNavigator(kb_retrieval_service=service)
    docs = await navigator.search_entries("考勤", ["kb-1"])

    assert {c["source_kind"] for c in service.calls} == {"wiki", "wiki_syn"}
    assert len(docs) == 2
    assert docs[0].metadata["page_links"] == ["远程办公", "请假制度"]
    assert docs[1].metadata["page_links"] == ["考勤管理"]


async def test_get_page_by_title_joins_chunks_and_truncates(patch_db, monkeypatch):
    """按页取读按 chunk_index 拼接全文，超过上限截断并提示。"""
    from src.config import settings

    monkeypatch.setattr(
        settings.wiki_compile, "WIKI_NAV_PAGE_MAX_CHARS", 5
    )
    navigator = WikiNavigator(
        vector_store=FakeVectorStore(
            {
                "page-1": [
                    FakeDoc("第二段", {"chunk_index": 1}),
                    FakeDoc("第一段", {"chunk_index": 0}),
                ]
            }
        )
    )
    page = await navigator.get_page_by_title(["kb-1"], "考勤管理")
    assert page is not None
    assert page["page_id"] == "page-1"
    # 上限 5 字符：拼接按 chunk_index 排序（第一段在前），超出部分截断并提示
    assert page["content"].startswith("第一段")
    assert "已截断" in page["content"]
    assert page["links"] == ["远程办公", "请假制度"]

    # 归一化标题命中 + 未命中
    assert await navigator.get_page_by_title(["kb-1"], "考勤管理 ") is not None
    assert await navigator.get_page_by_title(["kb-1"], "不存在的页") is None


def test_format_related_dedup_excludes_and_truncates():
    """Related 格式化：去重、排除命中页自身、上限截断、空链接返回空串。"""
    assert format_related([]) == ""
    out = format_related(["A", "B", "A", "self"], exclude_titles=["self"])
    assert "[[A]]" in out and "[[B]]" in out
    assert out.count("[[A]]") == 1
    assert "self" not in out
    many = format_related([f"P{i}" for i in range(20)])
    assert many.count("[[") == 8  # MAX_RELATED


async def _fake_nav(entry_docs, page):
    """构造 async 方法齐全的假导航器。"""

    async def search_entries(*a, **k):
        return list(entry_docs)

    async def get_page_by_title(*a, **k):
        return page

    return SimpleNamespace(search_entries=search_entries, get_page_by_title=get_page_by_title)


async def test_tool_entry_mode_outputs_related():
    """工具入口模式：输出含切片与 Related 行，sources 结构完整。"""
    navigator = await _fake_nav(
        [
            FakeDoc(
                "考勤页内容",
                {
                    "document_id": "page-1",
                    "source_kind": "wiki",
                    "heading_path": "考勤管理",
                    "source": "wiki://考勤管理",
                    "page_links": ["远程办公"],
                },
            )
        ],
        None,
    )
    tool = WikiLookupTool(kb_ids=["kb-1"], navigator=navigator)
    result = await tool.execute(query="考勤")
    assert result.success is not False
    assert "考勤页内容" in result.output
    assert "Related: [[远程办公]]" in result.output
    assert result.sources[0]["document_id"] == "page-1"


async def test_tool_page_title_mode_returns_full_page():
    """工具按页取读模式：返回全文与 Related。"""
    navigator = await _fake_nav(
        [],
        {
            "page_id": "page-1",
            "kb_id": "kb-1",
            "title": "考勤管理",
            "page_type": "entity",
            "content": "# 考勤管理\n每日打卡。",
            "links": ["远程办公"],
        },
    )
    tool = WikiLookupTool(kb_ids=["kb-1"], navigator=navigator)
    result = await tool.execute(page_title="考勤管理")
    assert "[实体页] 考勤管理" in result.output
    assert "每日打卡" in result.output
    assert "Related: [[远程办公]]" in result.output
    assert result.sources[0]["document_id"] == "page-1"


async def test_tool_page_title_not_found():
    navigator = await _fake_nav([], None)
    tool = WikiLookupTool(kb_ids=["kb-1"], navigator=navigator)
    result = await tool.execute(page_title="不存在")
    assert "未找到" in result.output


async def test_tool_validates_params_and_kb():
    """query/page_title 均空报错；未选知识库给友好提示。"""
    tool = WikiLookupTool(kb_ids=["kb-1"], navigator=await _fake_nav([], None))
    result = await tool.execute()
    assert result.success is False

    no_kb = WikiLookupTool(kb_ids=[], navigator=await _fake_nav([], None))
    result = await no_kb.execute(query="考勤")
    assert "未选择知识库" in result.output


# ----------------------------------------------------------------------
# E-1 W1 索引页优先入口
# ----------------------------------------------------------------------
INDEX_PAGE = SimpleNamespace(
    id="idx-1",
    kb_id="kb-1",
    title="索引",
    page_type="index",
    content_path="wiki/kb-1/idx-1.md",
    links=[],
    status="active",
)


class FakeMinio:
    def __init__(self, text):
        self._text = text

    async def download_text_async(self, key):
        return self._text


def _patch_minio(monkeypatch, text):
    import src.services.minio_service as minio_module

    async def _get_instance():
        return FakeMinio(text)

    monkeypatch.setattr(minio_module.MinioService, "get_instance", _get_instance)


def _session_maker_for(rows):
    class _DB:
        async def execute(self, *_a, **_k):
            return FakeResult(rows)

    class _SM:
        def __call__(self):
            return self

        async def __aenter__(self):
            return _DB()

        async def __aexit__(self, *_a):
            return False

    return _SM()


async def test_get_index_page_reads_content_from_minio(monkeypatch):
    """索引页从 PG 定位、MinIO 直读正文（不进向量库）。"""
    monkeypatch.setattr(
        "src.database.async_session_maker", _session_maker_for([INDEX_PAGE])
    )
    _patch_minio(monkeypatch, "# 知识库索引\n- [[考勤管理]]")
    navigator = WikiNavigator()  # 索引页不经向量库，无需注入 vector_store
    page = await navigator.get_index_page(["kb-1"])
    assert page is not None
    assert page["page_type"] == "index"
    assert page["page_id"] == "idx-1"
    assert "[[考勤管理]]" in page["content"]

    assert await navigator.get_index_page([]) is None


async def test_get_index_page_none_when_absent(monkeypatch):
    """无索引页时返回 None（fail-soft）。"""
    monkeypatch.setattr("src.database.async_session_maker", _session_maker_for([]))
    navigator = WikiNavigator()
    assert await navigator.get_index_page(["kb-1"]) is None


async def test_get_page_by_title_index_reads_minio(monkeypatch):
    """按标题取读索引页同样走 MinIO 直读（绕开向量库）。"""
    monkeypatch.setattr(
        "src.database.async_session_maker", _session_maker_for([INDEX_PAGE])
    )
    _patch_minio(monkeypatch, "# 知识库索引")
    navigator = WikiNavigator()
    page = await navigator.get_page_by_title(["kb-1"], "索引")
    assert page is not None
    assert "知识库索引" in page["content"]


async def _fake_nav_index(entry_docs, index_page):
    """带 get_index_page 的假导航器。"""

    async def search_entries(*a, **k):
        return list(entry_docs)

    async def get_page_by_title(*a, **k):
        return None

    async def get_index_page(*a, **k):
        return index_page

    return SimpleNamespace(
        search_entries=search_entries,
        get_page_by_title=get_page_by_title,
        get_index_page=get_index_page,
    )


FAKE_INDEX_DICT = {
    "page_id": "idx-1",
    "kb_id": "kb-1",
    "title": "索引",
    "page_type": "index",
    "content": "# 知识库索引\n- [[考勤管理]]",
    "links": [],
}


async def test_tool_entry_prepends_index_page_when_enabled(monkeypatch):
    """开关开启时，入口检索把索引页目录作为第一跳先行透出。"""
    from src.config import settings

    monkeypatch.setattr(
        settings.wiki_compile, "WIKI_NAV_INDEX_FIRST_ENABLED", True
    )
    navigator = await _fake_nav_index(
        [
            FakeDoc(
                "考勤页内容",
                {
                    "document_id": "page-1",
                    "source_kind": "wiki",
                    "heading_path": "考勤管理",
                    "source": "wiki://考勤管理",
                    "page_links": ["远程办公"],
                },
            )
        ],
        FAKE_INDEX_DICT,
    )
    tool = WikiLookupTool(kb_ids=["kb-1"], navigator=navigator)
    result = await tool.execute(query="考勤")
    assert "[索引页] 索引" in result.output
    assert "[[考勤管理]]" in result.output
    # 索引页 source 在首位
    assert result.sources[0]["document_id"] == "idx-1"


async def test_tool_entry_index_only_fallback_when_no_docs(monkeypatch):
    """无语义命中但有索引页时，返回目录而非「暂无相关」提示。"""
    from src.config import settings

    monkeypatch.setattr(
        settings.wiki_compile, "WIKI_NAV_INDEX_FIRST_ENABLED", True
    )
    navigator = await _fake_nav_index([], FAKE_INDEX_DICT)
    tool = WikiLookupTool(kb_ids=["kb-1"], navigator=navigator)
    result = await tool.execute(query="任意主题")
    assert "暂无相关" not in result.output
    assert "[索引页] 索引" in result.output


async def test_tool_entry_index_disabled_by_default(monkeypatch):
    """开关关闭时入口检索不读索引页（行为与 D1 一致）。"""
    from src.config import settings

    monkeypatch.setattr(
        settings.wiki_compile, "WIKI_NAV_INDEX_FIRST_ENABLED", False
    )
    navigator = await _fake_nav_index(
        [
            FakeDoc(
                "考勤页内容",
                {
                    "document_id": "page-1",
                    "source_kind": "wiki",
                    "heading_path": "考勤管理",
                    "source": "wiki://考勤管理",
                    "page_links": ["远程办公"],
                },
            )
        ],
        FAKE_INDEX_DICT,
    )
    tool = WikiLookupTool(kb_ids=["kb-1"], navigator=navigator)
    result = await tool.execute(query="考勤")
    assert "索引页" not in result.output
    assert "Related: [[远程办公]]" in result.output


# ----------------------------------------------------------------------
# E-1 W3 导航埋点
# ----------------------------------------------------------------------
def test_summarize_wiki_nav():
    """导航事件汇总：轮数 / 入口类型序列 / 取页序列 / 索引入口轮数。"""
    from src.services.pipeline.state import summarize_wiki_nav

    assert summarize_wiki_nav(None) is None
    assert summarize_wiki_nav([]) is None

    events = [
        {"mode": "entry", "target": "考勤", "index_used": True},
        {"mode": "page", "target": "考勤管理", "index_used": False},
        {"mode": "page", "target": "请假制度", "index_used": False},
    ]
    summary = summarize_wiki_nav(events)
    assert summary["rounds"] == 3
    assert summary["entry_modes"] == ["entry", "page", "page"]
    assert summary["page_sequence"] == ["考勤管理", "请假制度"]
    assert summary["index_rounds"] == 1


async def test_tool_records_wiki_nav_events(monkeypatch):
    """工具调用写入请求级导航缓冲：入口（经索引页）与按页取读各一条。"""
    from src.config import settings
    from src.services.pipeline import state as pipeline_state

    monkeypatch.setattr(
        settings.wiki_compile, "WIKI_NAV_INDEX_FIRST_ENABLED", True
    )
    navigator = await _fake_nav_index([], FAKE_INDEX_DICT)
    tool = WikiLookupTool(kb_ids=["kb-1"], navigator=navigator)

    token = pipeline_state._request_wiki_nav.set([])
    try:
        await tool.execute(query="考勤")            # 入口检索（经索引页目录）
        await tool.execute(page_title="考勤管理")   # 按页取读
        events = pipeline_state._request_wiki_nav.get()
    finally:
        pipeline_state._request_wiki_nav.reset(token)

    assert len(events) == 2
    assert events[0] == {"mode": "entry", "target": "考勤", "index_used": True}
    assert events[1]["mode"] == "page"
    assert events[1]["target"] == "考勤管理"


async def test_tool_nav_record_noop_without_buffer():
    """无请求级缓冲时（单测直调）记录埋点不报错、不影响工具结果。"""
    from src.services.pipeline import state as pipeline_state

    # 确保无缓冲
    assert pipeline_state._request_wiki_nav.get() is None
    navigator = await _fake_nav_index([], FAKE_INDEX_DICT)
    tool = WikiLookupTool(kb_ids=["kb-1"], navigator=navigator)
    result = await tool.execute(page_title="考勤管理")
    assert result.tool_name == "wiki_lookup"
