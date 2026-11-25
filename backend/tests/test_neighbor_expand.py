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


class _FakeSectionMilvus:
    """整节补查的假实现：按 `(document_id, 前缀)` 过滤，记录调用参数。"""

    def __init__(self, rows=None, exc=None, vectors=None):
        self.rows = rows or []
        self.exc = exc
        self.calls = []
        self.embeddings = _FakeEmbeddings(vectors or {})

    async def fetch_chunks_by_heading_prefix(self, prefixes):
        self.calls.append(list(prefixes))
        if self.exc:
            raise self.exc
        out = []
        for doc, prefix in prefixes:
            for r in self.rows:
                if r["document_id"] == doc and str(r["heading_path"]).startswith(f"{prefix} > "):
                    out.append(r)
        return out


class _FakeEmbeddings:
    """按表查向量；没登记过的文本直接报错，避免测试悄悄用上未预期的标题。"""

    def __init__(self, table):
        self.table = table

    async def aembed_documents(self, texts):
        return [self.table[t] for t in texts]


def _sec_vs(rows=None, exc=None, vectors=None):
    vs = VectorStoreManager()
    vs.milvus_service = _FakeSectionMilvus(rows, exc, vectors)
    return vs


H1 = "Runbook"
SEC = f"{H1} > 4. 恢复"


def _sec_doc(idx, text, score, sub="4.1 恢复顺序"):
    return _doc("d1", idx, text, score, heading_path=f"{SEC} > {sub}")


def _sec_row(idx, text, sub="4.1 恢复顺序"):
    row = _row("d1", idx, text)
    row["heading_path"] = f"{SEC} > {sub}"
    return row


class TestExpandSections:
    def test_appends_whole_section_in_chunk_order(self):
        rows = [
            _sec_row(28, "4.2 恢复后必做：清 semcache / 核维度", sub="4.2 恢复后必做"),
            _sec_row(24, "4.1 第 1~6 步"),
            _sec_row(26, "4.1 剩余步骤"),
        ]
        vs = _sec_vs(rows)
        base = [_sec_doc(25, "4.1 第 7~8 步", 0.5)]
        out = asyncio.run(vs.expand_sections(base, budget_chars=4000))

        assert [d.page_content for d in out] == [
            "4.1 第 7~8 步",
            "4.1 第 1~6 步",
            "4.1 剩余步骤",
            "4.2 恢复后必做：清 semcache / 核维度",
        ]

    def test_section_chunks_inherit_parent_score_and_flag(self):
        vs = _sec_vs([_sec_row(28, "4.2 恢复后必做", sub="4.2 恢复后必做")])
        base = [_sec_doc(25, "4.1", 0.5)]
        out = asyncio.run(vs.expand_sections(base, budget_chars=4000))
        added = out[1]
        assert added.metadata["section_expand"] is True
        assert added.metadata["rerank_score"] == 0.5
        assert added.metadata["score"] == 0.5

    def test_base_documents_are_the_same_objects(self):
        vs = _sec_vs([_sec_row(24, "x")])
        base = [_sec_doc(25, "原地块", 0.9)]
        out = asyncio.run(vs.expand_sections(base, budget_chars=4000))
        assert out[0] is base[0]

    def test_fetch_failure_returns_base_unchanged(self):
        vs = _sec_vs(exc=RuntimeError("milvus timeout"))
        base = [_sec_doc(25, "原地块", 0.9)]
        assert asyncio.run(vs.expand_sections(base, budget_chars=4000)) == base

    def test_two_level_heading_does_not_query(self):
        """只有两级标题时前缀为空（回填等于整篇文档），必须一次都不查。"""
        vs = _sec_vs([])
        base = [_doc("d1", 5, "x", 0.9, heading_path=f"{H1} > 4. 恢复")]
        assert asyncio.run(vs.expand_sections(base, budget_chars=4000)) == base
        assert vs.milvus_service.calls == []

    def test_budget_zero_disables(self):
        vs = _sec_vs([_sec_row(24, "x")])
        base = [_sec_doc(25, "原地块", 0.9)]
        assert asyncio.run(vs.expand_sections(base, budget_chars=0)) == base
        assert vs.milvus_service.calls == []

    def test_max_sections_caps_queries(self):
        vs = _sec_vs([])
        # 五个块分属五个不同小节：上限 2 时只能查两个前缀
        base = [
            _doc("d1", i, "x", 0.9, heading_path=f"A > {i}. 小节 > {i}.1 子节")
            for i in range(1, 6)
        ]
        asyncio.run(vs.expand_sections(base, budget_chars=4000, max_sections=2))
        assert vs.milvus_service.calls == [[("d1", "A > 1. 小节"), ("d1", "A > 2. 小节")]]

    def test_budget_limits_total_context(self):
        rows = [_sec_row(24, "甲" * 3000)]
        vs = _sec_vs(rows)
        base = [_sec_doc(25, "乙" * 500, 0.5)]
        out = asyncio.run(vs.expand_sections(base, budget_chars=1000))
        assert len(out) == 1  # 3000 字符的整节块被预算挡住

    def test_round_robin_gives_every_section_a_turn(self):
        """预算只够补两块时，高分表格小节与低分正文小节各拿一块。

        不轮转的话，高分小节会连拿两块，正文小节一块都拿不到——这正是
        §4.2 被 7.3 演练记录挤掉的形态。
        """
        table_sec = "RB > 7. 演练（每季度）"
        rows = []
        for idx in (110, 111, 112):
            row = _row("d1", idx, "表" * 400)
            row["heading_path"] = f"{table_sec} > 7.3 演练记录"
            rows.append(row)
        rows.append(_sec_row(26, "4.1 剩余步骤"))

        vs = _sec_vs(rows)
        base = [
            _doc("d1", 113, "表头", 0.746, heading_path=f"{table_sec} > 7.3 演练记录"),
            _sec_doc(25, "4.1 第 7~8 步", 0.5),
        ]
        out = asyncio.run(vs.expand_sections(base, budget_chars=430))
        contents = [d.page_content for d in out]
        assert "4.1 剩余步骤" in contents          # 低分小节也拿到了名额
        assert contents.count("表" * 400) == 1      # 高分小节没有连拿

    def test_far_chunks_of_same_section_are_skipped(self):
        """同一小节里相距 40 块的表格行属于另一个子主题，不该被补进来。"""
        table_sec = "RB > 7. 演练（每季度）"
        far = _row("d1", 70, "很远" * 5)
        far["heading_path"] = f"{table_sec} > 7.3 演练记录"
        vs = _sec_vs([far])
        base = [_doc("d1", 113, "表头", 0.746, heading_path=f"{table_sec} > 7.3 演练记录")]
        out = asyncio.run(vs.expand_sections(base, budget_chars=5000, max_distance=12))
        assert out == base

    def test_high_score_skips_expansion(self):
        """首块重排分够高说明已经找对了，不再按结构铺开（省上下文）。"""
        vs = _sec_vs([_sec_row(24, "整节块")])
        base = [_sec_doc(25, "答案就在这块", 0.972)]
        out = asyncio.run(vs.expand_sections(base, budget_chars=5000, skip_score=0.9))
        assert out == base
        assert vs.milvus_service.calls == []  # 连补查都不发

    def test_medium_score_still_expands(self):
        """0.746 这种「可能是空壳」的分数不足以判定已找准，仍要回填。"""
        vs = _sec_vs([_sec_row(24, "整节块")])
        base = [_sec_doc(25, "半截答案", 0.746)]
        out = asyncio.run(vs.expand_sections(base, budget_chars=5000, skip_score=0.9))
        assert [d.page_content for d in out] == ["半截答案", "整节块"]

    def test_skip_ignores_inherited_scores(self):
        """邻块继承来的 0.97 不能触发跳过——那等于自己给自己投票。"""
        vs = _sec_vs([_sec_row(24, "整节块")])
        base = [_sec_doc(25, "原始块 0.5", 0.5)]
        neighbor = _doc("d1", 26, "邻块", 0.97, neighbor_expand=True)
        out = asyncio.run(
            vs.expand_sections(base + [neighbor], budget_chars=5000, skip_score=0.9)
        )
        assert "整节块" in [d.page_content for d in out]

    def test_skip_disabled_when_threshold_none(self):
        vs = _sec_vs([_sec_row(24, "整节块")])
        base = [_sec_doc(25, "答案", 0.99)]
        out = asyncio.run(vs.expand_sections(base, budget_chars=5000, skip_score=None))
        assert len(out) == 2

    def test_min_section_score_drops_offtopic_section(self):
        """只补够分的小节：沾边的回滚小节（0.338）不该和正文小节（0.5）一起铺开。

        实测代价：两个小节都铺时 16 块里 9 块来自回滚小节，答案从 7 步缩成 3 行。
        """
        rollback = f"{H1} > 5. 回滚"
        rows = []
        for idx in (30, 31, 32):
            row = _row("d1", idx, f"决策矩阵行{idx}")
            row["heading_path"] = f"{rollback} > 5.1 决策矩阵"
            rows.append(row)
        rows.append(_sec_row(26, "4.1 剩余步骤"))
        rows.append(_sec_row(28, "4.2 恢复后必做", sub="4.2 恢复后必做"))

        vs = _sec_vs(rows)
        base = [
            _doc("d1", 39, "5.3 迁移回滚", 0.338, heading_path=f"{rollback} > 5.3 数据库迁移回滚"),
            _sec_doc(25, "4.1 第 7~8 步", 0.5),
        ]
        out = asyncio.run(vs.expand_sections(base, budget_chars=5000, min_section_score=0.4))
        contents = [d.page_content for d in out]
        assert "4.1 剩余步骤" in contents
        assert "4.2 恢复后必做" in contents
        assert not any(c.startswith("决策矩阵行") for c in contents)

    def test_min_section_score_none_keeps_all(self):
        rollback = f"{H1} > 5. 回滚"
        row = _row("d1", 30, "决策矩阵行")
        row["heading_path"] = f"{rollback} > 5.1 决策矩阵"
        vs = _sec_vs([row])
        base = [
            _doc("d1", 39, "5.3", 0.338, heading_path=f"{rollback} > 5.3 数据库迁移回滚"),
        ]
        out = asyncio.run(vs.expand_sections(base, budget_chars=5000, min_section_score=None))
        assert len(out) == 2
        """预算有限时先补「紧挨着的块」，而不是让排名第一的小节把预算吃满。

        实测原型：7.3 演练记录（0.746，表格行，离原地块 40+ 块远）与 4.1（0.5，
        紧邻 4.2）。按小节顺序排会把预算全给前者，4.2 永远进不来。
        """
        table_sec = "RB > 7. 演练（每季度）"
        rows = []
        # 排名第一的表格小节：远处的兄弟行，内容很长
        for idx in (70, 71, 72):
            row = _row("d1", idx, "表" * 400)
            row["heading_path"] = f"{table_sec} > 7.3 演练记录"
            rows.append(row)
        # 结构上紧邻的 4.2
        rows.append(_sec_row(28, "4.2 恢复后必做", sub="4.2 恢复后必做"))

        vs = _sec_vs(rows)
        base = [
            _doc("d1", 113, "演练记录表头", 0.746,
                 heading_path=f"{table_sec} > 7.3 演练记录"),
            _sec_doc(25, "4.1 第 7~8 步", 0.5),
        ]
        out = asyncio.run(vs.expand_sections(base, budget_chars=300))
        contents = [d.page_content for d in out]
        assert "4.2 恢复后必做" in contents
        # 远处的表格行被预算挡住，紧邻的 4.2 没有被挤掉
        assert "表" * 400 not in contents


class TestGateIgnoresSectionChunks:
    def test_average_is_computed_on_base_only(self):
        base = [_sec_doc(25, "高分块", 0.9), _sec_doc(39, "低分块", 0.2)]
        expanded = [_sec_doc(28, "整节块", 0.9)]
        expanded[0].metadata["section_expand"] = True
        avg, _ = asyncio.run(_Chain()._calculate_relevance("q", base + expanded))
        assert avg == pytest.approx(0.55)


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


class TestTitleGapGate:
    """子节标题闸门：只看「未命中子节标题是否比已命中的更贴问题」。

    向量用 2 维手写，cos 值可直接心算：
      [1,0] 与问题同向 → 1.0；[0,1] 正交 → 0.0。
    """

    Q = "恢复后要确认什么"
    VEC = {
        "恢复后要确认什么": [1.0, 0.0],
        "恢复后必做": [1.0, 0.0],   # 与问题同向
        "恢复顺序": [0.0, 1.0],     # 与问题正交
        "部分恢复": [0.0, 1.0],
    }

    def _rows(self):
        return [
            _sec_row(24, "第一步", sub="4.1 恢复顺序"),
            _sec_row(25, "第二步", sub="4.1 恢复顺序"),
            _sec_row(28, "确认清单", sub="4.2 恢复后必做"),
            _sec_row(29, "单库恢复", sub="4.3 部分恢复"),
        ]

    def test_expands_when_unhit_subsection_matches_question(self):
        vs = _sec_vs(self._rows(), vectors=self.VEC)
        base = [_sec_doc(24, "第一步", 0.5)]
        out = asyncio.run(
            vs.expand_sections(base, budget_chars=5000, query=self.Q, title_gap=0.15)
        )
        # 未命中的「恢复后必做」1.0 - 已命中的「恢复顺序」0.0 = 1.0 >= 0.15 → 铺
        assert len(out) == 4

    def test_skips_when_hit_subsection_already_matches(self):
        vec = dict(self.VEC)
        vec["恢复后必做"] = [0.0, 1.0]   # 未命中不贴问题
        vs = _sec_vs(self._rows(), vectors=vec)
        base = [_sec_doc(24, "第一步", 0.5)]
        out = asyncio.run(
            vs.expand_sections(base, budget_chars=5000, query=self.Q, title_gap=0.15)
        )
        # 1.0（已命中）− 0.0（未命中）= −1.0 < 0.15 → 不铺
        assert out == base

    def test_gate_off_keeps_old_behaviour(self):
        vs = _sec_vs(self._rows(), vectors=self.VEC)
        base = [_sec_doc(24, "第一步", 0.5)]
        out = asyncio.run(vs.expand_sections(base, budget_chars=5000, query=self.Q))
        assert len(out) == 4  # title_gap=None 时不启用闸门

    def test_no_unhit_subsection_means_skip(self):
        """该节所有子节都命中了，补不出新内容 —— 即使闸门开着也不该铺。"""
        vs = _sec_vs(self._rows(), vectors=self.VEC)
        base = [
            _sec_doc(24, "第一步", 0.5),
            _sec_doc(28, "确认清单", 0.5, sub="4.2 恢复后必做"),
            _sec_doc(29, "单库恢复", 0.5, sub="4.3 部分恢复"),
        ]
        out = asyncio.run(
            vs.expand_sections(base, budget_chars=5000, query=self.Q, title_gap=0.15)
        )
        assert [d.metadata["chunk_index"] for d in out] == [24, 28, 29]

    def test_embedding_failure_degrades_to_expand(self):
        """算不出差值时必须退回「照常铺」，不能变成「都不铺」。"""
        vs = _sec_vs(self._rows(), vectors={})  # 空表 → 查向量直接 KeyError
        base = [_sec_doc(24, "第一步", 0.5)]
        out = asyncio.run(
            vs.expand_sections(base, budget_chars=5000, query=self.Q, title_gap=0.15)
        )
        assert len(out) == 4

    def test_blank_query_does_not_gate(self):
        vs = _sec_vs(self._rows(), vectors=self.VEC)
        base = [_sec_doc(24, "第一步", 0.5)]
        out = asyncio.run(vs.expand_sections(base, budget_chars=5000, query="", title_gap=0.15))
        assert len(out) == 4
