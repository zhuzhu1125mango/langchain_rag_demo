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
