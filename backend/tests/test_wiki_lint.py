"""Wiki 体检报告单元测试（P3，§11.3，规则表驱动，零 LLM）。

覆盖：5 项规则正反用例、空库空报告、报告序列化。
"""

from types import SimpleNamespace

import pytest

from src.services.wiki_lint import (
    RULE_ABNORMAL_SIZE,
    RULE_BROKEN_LINK,
    RULE_EMPTY_SOURCE,
    RULE_MISSING_IN_INDEX,
    RULE_ORPHAN_PAGE,
    lint_kb_wiki,
)


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


def page(pid, title, page_type="entity", links=None, source=("d1",), content=""):
    return SimpleNamespace(
        id=pid,
        kb_id="kb-1",
        page_type=page_type,
        title=title,
        links=list(links or []),
        source_doc_ids=list(source),
        status="active",
        content_path=f"wiki/kb-1/{pid}.md",
    )


@pytest.fixture
def patched_minio(monkeypatch):
    contents = {}

    class FakeMinio:
        async def download_text_async(self, path):
            return contents.get(path, "")

    async def fake_get():
        return FakeMinio()

    monkeypatch.setattr("src.services.minio_service.MinioService.get_instance", fake_get)
    return contents


def rules_of(report):
    return [i.rule for i in report.issues]


class TestLintRules:
    async def test_empty_kb_returns_empty_report(self, patched_minio):
        report = await lint_kb_wiki(FakeDB([[]]), "kb-1")
        assert report.checked_pages == 0
        assert report.issues == []

    async def test_broken_link(self, patched_minio):
        """断链：links 指向的 title 无对应 active 页。"""
        rows = [
            page("idx", "索引", page_type="index", content="目录"),
            page("p1", "页面A", links=["幽灵页"]),
        ]
        patched_minio["wiki/kb-1/idx.md"] = "# 索引\n- [[页面A]]"
        patched_minio["wiki/kb-1/p1.md"] = "正文" * 100  # 正常体量，隔离其他规则

        report = await lint_kb_wiki(FakeDB([[*rows]]), "kb-1")

        broken = [i for i in report.issues if i.rule == RULE_BROKEN_LINK]
        assert len(broken) == 1
        assert broken[0].level == "error"
        assert broken[0].page_id == "p1"
        assert "幽灵页" in broken[0].message

    async def test_broken_link_negative_target_exists(self, patched_minio):
        rows = [
            page("idx", "索引", page_type="index", content="目录"),
            page("p1", "页面A", links=["页面B"]),
            page("p2", "页面B"),
        ]
        patched_minio["wiki/kb-1/idx.md"] = "# 索引\n- [[页面A]]\n- [[页面B]]"
        for r in (rows[1], rows[2]):
            patched_minio[r.content_path] = "正文" * 100

        report = await lint_kb_wiki(FakeDB([[*rows]]), "kb-1")
        assert RULE_BROKEN_LINK not in rules_of(report)

    async def test_orphan_page_and_missing_in_index(self, patched_minio):
        """孤立页与目录缺失：不在 index 目录且无入链。"""
        rows = [
            page("idx", "索引", page_type="index", content="目录"),
            page("p1", "页面A"),
        ]
        patched_minio["wiki/kb-1/idx.md"] = "# 索引\n（空目录）"
        patched_minio["wiki/kb-1/p1.md"] = "正文" * 100

        report = await lint_kb_wiki(FakeDB([[*rows]]), "kb-1")

        assert RULE_ORPHAN_PAGE in rules_of(report)
        assert RULE_MISSING_IN_INDEX in rules_of(report)
        orphan = next(i for i in report.issues if i.rule == RULE_ORPHAN_PAGE)
        assert orphan.level == "warning"

    async def test_orphan_negative_inbound_link_saves_page(self, patched_minio):
        """有其他页链接指向时不算孤立（但仍缺目录）。"""
        rows = [
            page("idx", "索引", page_type="index", content="目录"),
            page("p1", "页面A"),
            page("p2", "页面B", links=["页面A"]),
        ]
        patched_minio["wiki/kb-1/idx.md"] = "# 索引\n- [[页面B]]"
        for r in (rows[1], rows[2]):
            patched_minio[r.content_path] = "正文" * 100

        report = await lint_kb_wiki(FakeDB([[*rows]]), "kb-1")

        assert RULE_ORPHAN_PAGE not in rules_of(report)
        assert RULE_MISSING_IN_INDEX in rules_of(report)

    async def test_empty_source_page(self, patched_minio):
        """空源页：active 但 source_doc_ids=[]（防御性检查）。"""
        rows = [
            page("idx", "索引", page_type="index", content="目录"),
            page("p1", "页面A", source=[]),
        ]
        patched_minio["wiki/kb-1/idx.md"] = "# 索引\n- [[页面A]]"
        patched_minio["wiki/kb-1/p1.md"] = "正文" * 100

        report = await lint_kb_wiki(FakeDB([[*rows]]), "kb-1")

        empty = [i for i in report.issues if i.rule == RULE_EMPTY_SOURCE]
        assert len(empty) == 1
        assert empty[0].level == "warning"

    async def test_abnormal_size_short_and_long(self, patched_minio):
        rows = [
            page("idx", "索引", page_type="index", content="目录"),
            page("p1", "页面A"),
            page("p2", "页面B"),
        ]
        patched_minio["wiki/kb-1/idx.md"] = "# 索引\n- [[页面A]]\n- [[页面B]]"
        patched_minio["wiki/kb-1/p1.md"] = "短" * 100  # < 200 字
        patched_minio["wiki/kb-1/p2.md"] = "长" * 20001  # > 20000 字

        report = await lint_kb_wiki(FakeDB([[*rows]]), "kb-1")

        sizes = [i for i in report.issues if i.rule == RULE_ABNORMAL_SIZE]
        assert len(sizes) == 2
        assert all(i.level == "info" for i in sizes)

    async def test_index_page_itself_not_entity_linted(self, patched_minio):
        """索引页不参与实体/主题页规则（source_doc_ids 为空属正常）。"""
        rows = [page("idx", "索引", page_type="index", content="目录", source=[])]
        patched_minio["wiki/kb-1/idx.md"] = "# 索引\n- [[索引]]"

        report = await lint_kb_wiki(FakeDB([[*rows]]), "kb-1")

        assert report.issues == []
        assert report.checked_pages == 1


class TestReportSerialization:
    async def test_to_dict_shape(self, patched_minio):
        rows = [
            page("idx", "索引", page_type="index", content="目录"),
            page("p1", "页面A", links=["幽灵页"]),
        ]
        patched_minio["wiki/kb-1/idx.md"] = "# 索引\n- [[页面A]]"
        patched_minio["wiki/kb-1/p1.md"] = "正文" * 100

        report = await lint_kb_wiki(FakeDB([[*rows]]), "kb-1")
        data = report.to_dict()

        assert data["kb_id"] == "kb-1"
        assert data["checked_pages"] == 2
        issue = data["issues"][0]
        assert set(issue.keys()) == {"rule", "level", "page_id", "title", "message"}
