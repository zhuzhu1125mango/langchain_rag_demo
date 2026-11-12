"""邻块回填接入层的测试：Document 转换、父块分数继承、门禁隔离。

纯逻辑在 `test_chunk_expand.py`；这里钉的是「接进生产后」才会出现的三个坑：

1. 邻块要带父块的分数，否则下游按分数排序/过滤时它会莫名沉底；
2. 补查失败必须退化成「不回填」，不能把整次检索打成无结果；
3. 相关性门禁看不到回填块——否则平均分被复制过的分数带偏，阈值判定被悄悄改掉。
"""

import asyncio
import os
import sys

import pytest
from langchain_core.documents import Document

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.pipeline.retrieval import RetrievalMixin  # noqa: E402
from src.services.vector_store import VectorStoreManager  # noqa: E402


def _doc(doc_id, idx, text, score, **extra):
    meta = {
        "document_id": doc_id,
        "chunk_index": idx,
        "rerank_score": score,
        "score": score,
        "kb_id": "kb1",
        "source": "guide.md",
        "heading_path": "h",
    }
    meta.update(extra)
    return Document(page_content=text, metadata=meta)


class _FakeMilvus:
    """只实现补查接口；记录收到的位置，便于断言「不跨文档、不重复查」。"""

    def __init__(self, rows=None, exc=None):
        self.rows = rows or []
        self.exc = exc
        self.calls = []

    async def fetch_chunks_by_positions(self, positions):
        self.calls.append(list(positions))
        if self.exc:
            raise self.exc
        wanted = set(positions)
        return [r for r in self.rows if (r["document_id"], r["chunk_index"]) in wanted]


def _vs(rows=None, exc=None):
    vs = VectorStoreManager()
    vs.milvus_service = _FakeMilvus(rows, exc)
    return vs


def _row(doc_id, idx, text):
    return {
        "id": f"{doc_id}#{idx}",
        "document_id": doc_id,
        "chunk_index": idx,
        "content": text,
        "kb_id": "kb1",
        "source": "guide.md",
        "heading_path": "h",
    }


class TestExpandNeighbors:
    def test_appends_neighbor_with_parent_score(self):
        vs = _vs([_row("d1", 4, "上一块：SIGTERM 等待 30s")])
        base = [_doc("d1", 5, "优雅停机第一步", 0.87)]
        out = asyncio.run(vs.expand_neighbors(base, budget_chars=2000))

        assert [d.page_content for d in out] == ["优雅停机第一步", "上一块：SIGTERM 等待 30s"]
        nb = out[1]
        assert nb.metadata["neighbor_expand"] is True
        # 沿用父块的分数，而不是 0
        assert nb.metadata["rerank_score"] == 0.87
        assert nb.metadata["score"] == 0.87

    def test_base_documents_are_the_same_objects(self):
        """原地块不重建：重建会丢掉 filename 等上游补的元信息。"""
        vs = _vs([_row("d1", 6, "下一块")])
        base = [_doc("d1", 5, "原地块", 0.9)]
        out = asyncio.run(vs.expand_neighbors(base, budget_chars=2000))
        assert out[0] is base[0]

    def test_fetch_failure_returns_base_unchanged(self):
        """回填是增益不是依赖：补查炸了也必须返回原来的 3 块。"""
        vs = _vs(exc=RuntimeError("milvus timeout"))
        base = [_doc("d1", 5, "原地块", 0.9)]
        out = asyncio.run(vs.expand_neighbors(base, budget_chars=2000))
        assert out == base

    def test_budget_zero_disables(self):
        vs = _vs([_row("d1", 4, "邻居")])
        base = [_doc("d1", 5, "原地块", 0.9)]
        out = asyncio.run(vs.expand_neighbors(base, budget_chars=0))
        assert out == base
        assert vs.milvus_service.calls == []

    def test_requests_positions_around_each_base_chunk(self):
        vs = _vs([])
        base = [_doc("d1", 5, "a", 0.9), _doc("d1", 9, "b", 0.8)]
        asyncio.run(vs.expand_neighbors(base, budget_chars=2000))
        assert vs.milvus_service.calls == [[("d1", 4), ("d1", 6), ("d1", 8), ("d1", 10)]]

    def test_does_not_expand_when_position_missing(self):
        """元信息缺 document_id/chunk_index 时跳过，不去猜位置。"""
        vs = _vs([])
        base = [Document(page_content="x", metadata={"rerank_score": 0.5})]
        out = asyncio.run(vs.expand_neighbors(base, budget_chars=2000))
        assert out == base
        assert vs.milvus_service.calls == []


class _Chain(RetrievalMixin):
    def _rerank_score_available(self):
        return True


class TestGateIgnoresExpandedChunks:
    def test_average_is_computed_on_base_only(self):
        base = [_doc("d1", 5, "高分块", 0.9), _doc("d1", 9, "低分块", 0.2)]
        expanded = [
            _doc("d1", 4, "邻1", 0.9, neighbor_expand=True),
            _doc("d1", 6, "邻2", 0.9, neighbor_expand=True),
        ]
        avg, _ = asyncio.run(_Chain()._calculate_relevance("q", base + expanded))
        # 含邻块会是 (0.9+0.2+0.9+0.9)/4 = 0.725；只算原始块是 0.55
        assert avg == pytest.approx(0.55)

    def test_gate_identical_to_before_expansion(self):
        base = [_doc("d1", 5, "a", 0.9), _doc("d1", 9, "b", 0.2)]
        expanded = [_doc("d1", 4, "c", 0.9, neighbor_expand=True)]
        before = asyncio.run(_Chain()._calculate_relevance("q", base))
        after = asyncio.run(_Chain()._calculate_relevance("q", base + expanded))
        assert before == after
