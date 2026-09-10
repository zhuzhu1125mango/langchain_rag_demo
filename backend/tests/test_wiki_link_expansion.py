"""交叉链接检索扩展单元测试（P3，§11.2，DB/向量库全 mock）。

覆盖：开关两态、非 wiki 命中不扩展、命中追加（每页 2 块/总上限 6 块/
link_expanded 可追溯/不挤占原命中）、失败回退原结果。
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document

from src.config import settings
from src.services.wiki_link_expansion import expand_wiki_links


class FakeScalars:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items


class FakeResult:
    def __init__(self, items):
        self._scalars = FakeScalars(items)

    def scalars(self):
        return self._scalars


class FakeDB:
    def __init__(self, select_queue):
        self.select_queue = list(select_queue)

    async def execute(self, stmt):
        return FakeResult(self.select_queue.pop(0) if self.select_queue else [])


def wiki_doc(page_id: str, kb_id: str = "kb-1", score: float = 5.0) -> Document:
    return Document(
        page_content=f"wiki 页 {page_id} 内容",
        metadata={
            "source_kind": "wiki",
            "document_id": page_id,
            "kb_id": kb_id,
            "source": f"wiki://页面{page_id}",
            "chunk_index": 0,
            "score": score,
            "rerank_score": score,
        },
    )


def raw_doc() -> Document:
    return Document(page_content="普通文档", metadata={"source_kind": "raw", "document_id": "d1"})


def page_row(pid: str, kb_id: str, title: str, links) -> SimpleNamespace:
    return SimpleNamespace(
        id=pid, kb_id=kb_id, page_type="entity", title=title,
        links=links, status="active", content_path=f"wiki/{kb_id}/{pid}.md",
    )


@pytest.fixture
def patched_stack(monkeypatch):
    """返回 (install(db, vector_store) 安装器, 向量仓库引用容器)。"""
    holder = {}

    def install(select_queue, chunks_by_doc):
        db = FakeDB(select_queue)

        @asynccontextmanager
        async def fake_session():
            yield db

        monkeypatch.setattr("src.database.async_session_maker", lambda: fake_session())

        class FakeVectorStore:
            async def get_chunks_by_document_id(self, document_id):
                return list(chunks_by_doc.get(document_id, []))

        async def fake_vector_get():
            return FakeVectorStore()

        monkeypatch.setattr(
            "src.services.vector_store.VectorStoreManager.get_instance", fake_vector_get
        )
        holder["db"] = db
        return db

    return install, holder


def target_chunks(doc_id: str, n: int = 2) -> list:
    """模拟目标页分块（乱序返回，验证按 chunk_index 排序）。"""
    docs = [
        Document(
            page_content=f"{doc_id} 块{i}",
            metadata={"source_kind": "wiki", "document_id": doc_id,
                      "kb_id": "kb-1", "chunk_index": i, "score": 0.0},
        )
        for i in range(n)
    ]
    return list(reversed(docs))


class TestExpandWikiLinks:
    async def test_disabled_returns_original(self, patched_stack, monkeypatch):
        install, _ = patched_stack
        monkeypatch.setattr(settings.wiki_compile, "WIKI_LINK_EXPANSION", False)
        docs = [wiki_doc("w1")]
        assert await expand_wiki_links(docs) is docs

    async def test_no_wiki_hits_returns_original(self, patched_stack, monkeypatch):
        install, _ = patched_stack
        monkeypatch.setattr(settings.wiki_compile, "WIKI_LINK_EXPANSION", True)
        docs = [raw_doc()]
        assert await expand_wiki_links(docs) is docs

    async def test_hit_appends_linked_page_chunks(self, patched_stack, monkeypatch):
        install, _ = patched_stack
        monkeypatch.setattr(settings.wiki_compile, "WIKI_LINK_EXPANSION", True)
        install(
            select_queue=[
                [page_row("w1", "kb-1", "页面A", ["页面B"])],
                [page_row("w1", "kb-1", "页面A", []), page_row("w2", "kb-1", "页面B", [])],
            ],
            chunks_by_doc={"w2": target_chunks("w2", 2)},
        )
        docs = [wiki_doc("w1")]

        result = await expand_wiki_links(docs)

        assert len(result) == 3  # 原命中 + 2 扩展块
        assert result[0] is docs[0]  # 原命中在前不挤占
        appended = result[1:]
        assert all(d.metadata["link_expanded"] is True for d in appended)
        assert all(d.metadata["document_id"] == "w2" for d in appended)
        assert all(d.metadata["score"] == 0.0 for d in appended)
        # 乱序返回的块按 chunk_index 排序后截取
        assert [d.metadata["chunk_index"] for d in appended] == [0, 1]

    async def test_total_cap_six_chunks(self, patched_stack, monkeypatch):
        install, _ = patched_stack
        monkeypatch.setattr(settings.wiki_compile, "WIKI_LINK_EXPANSION", True)
        links = ["页面B", "页面C", "页面D", "页面E"]
        install(
            select_queue=[
                [page_row("w1", "kb-1", "页面A", links)],
                [page_row("w1", "kb-1", "页面A", [])]
                + [page_row(f"w{i}", "kb-1", t, []) for i, t in enumerate(links, start=2)],
            ],
            chunks_by_doc={f"w{i}": target_chunks(f"w{i}", 2) for i in range(2, 6)},
        )
        result = await expand_wiki_links([wiki_doc("w1")])

        assert len(result) == 1 + 6  # 总上限 6 块
        assert all(d.metadata["link_expanded"] for d in result[1:])

    async def test_self_and_duplicate_targets_skipped(self, patched_stack, monkeypatch):
        install, _ = patched_stack
        monkeypatch.setattr(settings.wiki_compile, "WIKI_LINK_EXPANSION", True)
        install(
            select_queue=[
                [page_row("w1", "kb-1", "页面A", ["页面A", "页面B", "页面B"])],
                [page_row("w1", "kb-1", "页面A", []), page_row("w2", "kb-1", "页面B", [])],
            ],
            chunks_by_doc={"w2": target_chunks("w2", 1)},
        )
        result = await expand_wiki_links([wiki_doc("w1")])

        # 自引用排除；重复目标只取一次 → 仅 1 块
        assert len(result) == 2
        assert result[1].metadata["document_id"] == "w2"

    async def test_empty_links_returns_original(self, patched_stack, monkeypatch):
        install, _ = patched_stack
        monkeypatch.setattr(settings.wiki_compile, "WIKI_LINK_EXPANSION", True)
        install(
            select_queue=[[page_row("w1", "kb-1", "页面A", [])], []],
            chunks_by_doc={},
        )
        docs = [wiki_doc("w1")]
        result = await expand_wiki_links(docs)
        assert result == docs
        assert len(result) == 1

    async def test_db_failure_returns_original(self, patched_stack, monkeypatch):
        install, _ = patched_stack
        monkeypatch.setattr(settings.wiki_compile, "WIKI_LINK_EXPANSION", True)

        @asynccontextmanager
        async def boom_session():
            raise RuntimeError("db down")
            yield  # pragma: no cover

        monkeypatch.setattr("src.database.async_session_maker", lambda: boom_session())
        docs = [wiki_doc("w1")]
        assert await expand_wiki_links(docs) == docs
