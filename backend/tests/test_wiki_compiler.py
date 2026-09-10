"""Wiki 编译器单元测试（P2 LLM-Wiki 编译层 Phase 1，LLM 全 mock）。

覆盖：候选抽取解析（含容错）、标题匹配（精确/embedding 降级）、
页数上限、持久化 upsert（新建/更新）、管线挂载（失败不阻断上传）。
"""

import asyncio
import uuid
from types import SimpleNamespace
from typing import List

import pytest

from src.config import settings
from src.services.wiki_compiler import (
    CompiledPage,
    ExistingPage,
    WikiCompileResult,
    WikiCompiler,
    _cosine,
    _parse_json_dict,
    _strip_think,
    build_material,
    normalize_doc_ids,
    normalize_title,
)


# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------
class TestTooling:
    def test_normalize_title(self):
        assert normalize_title("  RAG 系统  ") == "rag系统"

    def test_strip_think(self):
        assert _strip_think("<think>思考</think>答案") == "答案"
        assert _strip_think("无思考块") == "无思考块"

    def test_parse_json_variants(self):
        assert _parse_json_dict('{"pages": []}') == {"pages": []}
        assert _parse_json_dict('```json\n{"pages": []}\n```') == {"pages": []}
        assert _parse_json_dict('好的：{"pages": [1]} 完成') == {"pages": [1]}
        with pytest.raises(Exception):
            _parse_json_dict("完全不是 JSON")

    def test_cosine(self):
        assert _cosine([1, 0], [1, 0]) == pytest.approx(1.0)
        assert _cosine([1, 0], [0, 1]) == 0.0
        assert _cosine([0, 0], [1, 0]) == 0.0
        assert _cosine([1], [1, 2]) == 0.0
        assert _cosine([], []) == 0.0


# ----------------------------------------------------------------------
# LLM mock
# ----------------------------------------------------------------------
class FakeLLM:
    """按调用次序返回预设响应。"""

    def __init__(self, responses: List[str]):
        self.responses = list(responses)
        self.prompts: List[str] = []

    async def ainvoke(self, prompt: str):
        self.prompts.append(prompt)
        if not self.responses:
            raise RuntimeError("no more fake responses")
        return SimpleNamespace(content=self.responses.pop(0))


def make_compiler(responses: List[str], monkeypatch=None) -> WikiCompiler:
    compiler = WikiCompiler()
    fake = FakeLLM(responses)
    monkeypatch.setattr(compiler, "_llm", fake, raising=False)
    # _get_llm 懒加载会覆盖 _llm，直接短路
    monkeypatch.setattr(compiler, "_get_llm", lambda: fake)
    return compiler


EXTRACT_OK = (
    '{"pages": ['
    '{"title": "RAG架构", "type": "entity", "key_facts": ["检索增强生成"]},'
    '{"title": "向量检索", "type": "topic", "key_facts": []}'
    "]}"
)


class FakeChunks:
    @staticmethod
    def of(*texts: str):
        return [SimpleNamespace(page_content=t) for t in texts]


# ----------------------------------------------------------------------
# compile_pages：抽取 + 生成
# ----------------------------------------------------------------------
class TestCompilePages:
    async def test_empty_material_returns_empty(self, monkeypatch):
        compiler = make_compiler([], monkeypatch)
        assert await compiler.compile_pages([FakeChunks.of("  ", "")], []) == []

    async def test_extraction_failure_returns_empty(self, monkeypatch):
        compiler = make_compiler(["不是JSON输出"], monkeypatch)
        assert await compiler.compile_pages(FakeChunks.of("RAG 是检索增强生成"), []) == []

    async def test_extraction_retry_recovers_from_transient_json_error(self, monkeypatch):
        """回归（2026-09-10 运行时验收）：LLM JSON 偶发畸变时自动重试一次。

        真实环境实测 qwen3 会偶发输出截断/未转义引号，旧逻辑直接放弃导致
        该文档零页面；重试一次通常即可恢复（LLM 非确定性）。
        """
        compiler = make_compiler(
            ["畸变{输出", EXTRACT_OK, "# RAG架构\n\n内容A", "# 向量检索\n\n内容B"],
            monkeypatch,
        )
        pages = await compiler.compile_pages(FakeChunks.of("RAG 是检索增强生成"), [])
        assert len(pages) == 2
        # 抽取调用 2 次（1 失败 + 1 重试）+ 页面生成 2 次
        assert len(compiler._llm.prompts) == 4

    async def test_new_pages_generated(self, monkeypatch):
        compiler = make_compiler([EXTRACT_OK, "# RAG架构\n\n内容A", "# 向量检索\n\n内容B"], monkeypatch)
        pages = await compiler.compile_pages(FakeChunks.of("RAG 是检索增强生成"), [])
        assert len(pages) == 2
        assert pages[0].title == "RAG架构"
        assert pages[0].page_type == "entity"
        assert pages[0].matched is None
        assert pages[0].content.startswith("# RAG架构")

    async def test_exact_title_match_uses_merge_prompt(self, monkeypatch):
        existing = ExistingPage(page_id="p1", title="rag架构", page_type="entity", content="旧内容")
        compiler = make_compiler([EXTRACT_OK, "合并后的页面"], monkeypatch)
        pages = await compiler.compile_pages(FakeChunks.of("材料"), [existing])
        assert pages[0].matched is existing
        # 合并 prompt 应包含既有内容与矛盾标注约束
        assert "既有页面内容" in compiler._llm.prompts[1]
        assert "矛盾提示" in compiler._llm.prompts[1]

    async def test_page_generation_failure_skips_page(self, monkeypatch):
        # 第 2 页生成时响应耗尽 → 抛错跳过，第 1 页正常
        compiler = make_compiler([EXTRACT_OK, "# RAG架构"], monkeypatch)
        pages = await compiler.compile_pages(FakeChunks.of("材料"), [])
        assert len(pages) == 1
        assert pages[0].title == "RAG架构"

    async def test_max_pages_cap(self, monkeypatch):
        monkeypatch.setattr(
            settings.wiki_compile, "WIKI_COMPILE_MAX_PAGES_PER_DOC", 1
        )
        compiler = make_compiler([EXTRACT_OK, "# RAG架构"], monkeypatch)
        pages = await compiler.compile_pages(FakeChunks.of("材料"), [])
        assert len(pages) == 1

    async def test_embedding_match_falls_back_to_new_page_on_error(self, monkeypatch):
        class BoomEmbeddings:
            async def aembed_documents(self, texts):
                raise ConnectionError("ollama down")

        compiler = make_compiler([EXTRACT_OK, "# 向量检索", "# 无关条目"], monkeypatch)
        monkeypatch.setattr(compiler, "_embeddings", BoomEmbeddings(), raising=False)
        monkeypatch.setattr(compiler, "_get_embeddings", lambda: BoomEmbeddings())

        existing = ExistingPage(page_id="p1", title="完全不同的标题", page_type="topic", content="")
        pages = await compiler.compile_pages(FakeChunks.of("材料"), [existing])
        assert len(pages) == 2  # 未匹配 → 两页都按新页生成
        assert all(p.matched is None for p in pages)

    async def test_embedding_match_above_threshold(self, monkeypatch):
        class FakeEmbeddings:
            async def aembed_documents(self, texts):
                # 两个候选标题与既有页"检索增强生成"同向，"无关条目"正交
                table = {
                    "RAG架构": [1.0, 0.0],
                    "向量检索": [1.0, 0.0],
                    "无关条目": [0.0, 1.0],
                    "检索增强生成": [1.0, 0.0],
                }
                return [table[t] for t in texts]

        extract = (
            '{"pages": ['
            '{"title": "RAG架构", "type": "entity", "key_facts": []},'
            '{"title": "无关条目", "type": "topic", "key_facts": []}'
            "]}"
        )
        compiler = make_compiler([extract, "页A", "页B"], monkeypatch)
        monkeypatch.setattr(compiler, "_get_embeddings", lambda: FakeEmbeddings())

        existing = ExistingPage(page_id="p1", title="检索增强生成", page_type="entity", content="旧")
        pages = await compiler.compile_pages(FakeChunks.of("材料"), [existing])
        assert pages[0].matched is existing
        assert pages[1].matched is None


# ----------------------------------------------------------------------
# persist_pages：MinIO/DB/向量库（全 mock）
# ----------------------------------------------------------------------
class FakeScalars:
    def __init__(self, items):
        self._items = items

    def first(self):
        return self._items[0] if self._items else None

    def all(self):
        return self._items


class FakeResult:
    def __init__(self, items):
        self._scalars = FakeScalars(items)

    def scalars(self):
        return self._scalars


class FakeDB:
    """按调用次序弹出 select 结果的假会话。"""

    def __init__(self, select_queue):
        self.select_queue = list(select_queue)
        self.added = []
        self.commit_count = 0

    async def execute(self, stmt):
        return FakeResult(self.select_queue.pop(0) if self.select_queue else [])

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commit_count += 1


class FakeMinio:
    def __init__(self):
        self.uploads = {}

    async def upload_text_async(self, key, content):
        self.uploads[key] = content
        return key


class FakeVectorStore:
    def __init__(self):
        self.indexed = []
        self.deleted_doc_ids = []
        self.milvus_service = SimpleNamespace(
            delete_by_document_id=self._delete_by_document_id
        )

    async def _delete_by_document_id(self, document_id):
        self.deleted_doc_ids.append(document_id)

    async def add_documents(self, docs, kb_id):
        self.indexed.append((list(docs), kb_id))
        return len(docs)


@pytest.fixture
def patched_services(monkeypatch):
    minio = FakeMinio()
    vector = FakeVectorStore()

    async def fake_minio_get():
        return minio

    async def fake_vector_get():
        return vector

    monkeypatch.setattr("src.services.minio_service.MinioService.get_instance", fake_minio_get)
    monkeypatch.setattr("src.services.vector_store.VectorStoreManager.get_instance", fake_vector_get)
    return minio, vector


class TestPersistPages:
    async def test_new_page_creates_row_and_indexes(self, patched_services):
        minio, vector = patched_services
        # kb_id="kb-1" 非法 UUID → owner 查询跳过；调用序：页面 select → 索引页 select → 全页 select
        db = FakeDB(select_queue=[[], [], []])
        compiler = WikiCompiler()
        pages = [
            CompiledPage(title="RAG架构", page_type="entity", content="# RAG架构\n\n正文")
        ]

        result = await compiler.persist_pages(db, "kb-1", "doc-1", pages)

        assert (result.pages_created, result.pages_updated) == (1, 0)
        assert result.chunks_indexed == 1  # 短页面正文合并为单块；索引页不进向量库
        page_row = db.added[0]
        assert page_row.source_doc_ids == ["doc-1"]
        assert page_row.revision == 1
        assert page_row.content_path.startswith("wiki/kb-1/")
        assert page_row.content_path in minio.uploads
        # 索引页已创建
        index_row = db.added[-1]
        assert index_row.page_type == "index"
        # 向量入库携带 source_kind=wiki
        wiki_docs, kb_id = vector.indexed[0]
        assert kb_id == "kb-1"
        assert all(d.metadata["source_kind"] == "wiki" for d in wiki_docs)
        assert all(d.metadata["document_id"] == page_row.id for d in wiki_docs)

    async def test_add_documents_returning_none_does_not_break(self, patched_services):
        """回归（2026-09-10 运行时验收发现）：真实 add_documents 无返回值，
        persist_pages 不得因 `n_indexed += None` 抛错。"""
        _, vector = patched_services

        async def insert_no_return(docs, kb_id):
            vector.indexed.append((list(docs), kb_id))

        vector.add_documents = insert_no_return
        db = FakeDB(select_queue=[[], [], []])
        compiler = WikiCompiler()
        pages = [CompiledPage(title="RAG架构", page_type="entity", content="# RAG架构\n\n正文")]

        result = await compiler.persist_pages(db, "kb-1", "doc-1", pages)

        assert (result.pages_created, result.pages_updated) == (1, 0)
        assert result.chunks_indexed == 1  # 入库数按分块数计，不依赖 add_documents 返回值

    async def test_existing_page_upserts_revision_and_sources(self, patched_services):
        minio, _ = patched_services
        existing_row = SimpleNamespace(
            id="page-1",
            kb_id="kb-1",
            page_type="entity",
            title="RAG架构",
            content_path="wiki/kb-1/page-1.md",
            source_doc_ids=["doc-0"],
            revision=1,
            status="active",
            owner_id=None,
        )
        # 调用序：页面 select（命中既有行）→ 索引页 select → 全页 select
        db = FakeDB(select_queue=[[existing_row], [], []])
        compiler = WikiCompiler()
        pages = [CompiledPage(title="RAG架构", page_type="entity", content="更新内容")]

        result = await compiler.persist_pages(db, "kb-1", "doc-1", pages)

        assert (result.pages_created, result.pages_updated) == (0, 1)
        assert existing_row.revision == 2
        assert existing_row.source_doc_ids == ["doc-0", "doc-1"]
        assert minio.uploads["wiki/kb-1/page-1.md"] == "更新内容"

    async def test_existing_page_deletes_old_vectors_before_reindex(self, patched_services):
        """既有页更新先删旧向量，避免多 revision 重复累积（Phase 2 修复）。"""
        _, vector = patched_services
        existing_row = SimpleNamespace(
            id="page-1", kb_id="kb-1", page_type="entity", title="RAG架构",
            content_path="wiki/kb-1/page-1.md", source_doc_ids=["doc-0"],
            revision=1, status="active", owner_id=None,
        )
        db = FakeDB(select_queue=[[existing_row], [], []])
        compiler = WikiCompiler()
        await compiler.persist_pages(
            db, "kb-1", "doc-1",
            [CompiledPage(title="RAG架构", page_type="entity", content="更新内容")],
        )
        assert vector.deleted_doc_ids == ["page-1"]
        assert len(vector.indexed) == 1

    async def test_new_page_does_not_delete_vectors(self, patched_services):
        _, vector = patched_services
        db = FakeDB(select_queue=[[], [], []])
        compiler = WikiCompiler()
        await compiler.persist_pages(
            db, "kb-1", "doc-1",
            [CompiledPage(title="RAG架构", page_type="entity", content="内容")],
        )
        assert vector.deleted_doc_ids == []

    async def test_index_page_updated_not_created_when_exists(self, patched_services):
        minio, _ = patched_services
        existing_entity = SimpleNamespace(
            id="page-1", kb_id="kb-1", page_type="entity", title="RAG架构",
            content_path="wiki/kb-1/page-1.md", source_doc_ids=[], revision=3,
            status="active", owner_id=None,
        )
        existing_index = SimpleNamespace(
            id="idx-1", kb_id="kb-1", page_type="index", title="索引",
            content_path="wiki/kb-1/idx-1.md", source_doc_ids=[], revision=1,
            status="active", owner_id=None,
        )
        # 调用序：页面 select（命中）→ links 标题 select → 索引页 select（命中）→ 索引目录全页 select
        db = FakeDB(
            select_queue=[[existing_entity], [], [existing_index], [existing_entity]]
        )
        compiler = WikiCompiler()
        result = await compiler.persist_pages(
            db, "kb-1", "doc-1",
            [CompiledPage(title="RAG架构", page_type="entity", content="内容")],
        )
        assert result.pages_created == 0
        # 索引页走更新路径（revision+1），不复用 added
        assert existing_index.revision == 2
        assert "# 知识库索引" in minio.uploads["wiki/kb-1/idx-1.md"]

    async def test_vector_index_failure_does_not_break_persist(self, patched_services, monkeypatch):
        minio, vector = patched_services

        async def boom(docs, kb_id):
            raise ConnectionError("milvus down")

        monkeypatch.setattr(vector, "add_documents", boom)
        db = FakeDB(select_queue=[[], [], []])
        compiler = WikiCompiler()
        result = await compiler.persist_pages(
            db, "kb-1", "doc-1",
            [CompiledPage(title="RAG架构", page_type="entity", content="内容")],
        )
        assert result.pages_created == 1
        assert result.chunks_indexed == 0


# ----------------------------------------------------------------------
# 交叉链接提取（Phase 3）
# ----------------------------------------------------------------------
class TestExtractLinks:
    def test_basic_extraction_dedup(self):
        from src.services.wiki_compiler import extract_links

        valid = {normalize_title(t): t for t in ["页面A", "页面B"]}
        content = "见 [[页面A]] 与 [[页面A]]、[[页面B]]"
        assert extract_links(content, "本页", valid) == ["页面A", "页面B"]

    def test_self_reference_excluded(self):
        from src.services.wiki_compiler import extract_links

        valid = {normalize_title(t): t for t in ["本页", "页面A"]}
        content = "自引用 [[本页]] 与 [[页面A]]"
        assert extract_links(content, "本页", valid) == ["页面A"]

    def test_nonexistent_target_excluded(self):
        from src.services.wiki_compiler import extract_links

        valid = {normalize_title(t): t for t in ["页面A"]}
        assert extract_links("[[不存在]] [[页面A]]", "本页", valid) == ["页面A"]

    def test_no_links_or_empty_content(self):
        from src.services.wiki_compiler import extract_links

        assert extract_links("纯文本无链接", "本页", {}) == []
        assert extract_links("", "本页", {"a": "A"}) == []

    def test_set_input_accepted(self):
        from src.services.wiki_compiler import extract_links

        assert extract_links("[[页面A]]", "本页", {"页面A"}) == ["页面A"]


class TestFillLinks:
    async def test_persist_writes_links_for_batch(self, patched_services):
        """persist 后 links 列写入批次内互链（批次提交后再校验存在性）。"""
        # 调用序：页面 select×2 → links 标题 select（批次已提交，含批次内页）→ 索引页/全页 select
        db = FakeDB(select_queue=[[], [], ["页面A", "页面B"], [], []])
        compiler = WikiCompiler()
        pages = [
            CompiledPage(title="页面A", page_type="entity", content="见 [[页面B]] 与 [[不存在]]"),
            CompiledPage(title="页面B", page_type="topic", content="引用 [[页面A]] 与 [[页面B]]"),
        ]
        await compiler.persist_pages(db, "kb-1", "doc-1", pages)

        row_a, row_b = db.added[0], db.added[1]
        assert row_a.links == ["页面B"]  # 不存在页被过滤
        assert row_b.links == ["页面A"]  # 自引用被过滤

    async def test_links_failure_does_not_break_persist(self, patched_services):
        """links 提取失败（DB 异常）只告警，不影响编译产物与向量入库。"""
        _, vector = patched_services

        class FlakyDB(FakeDB):
            def __init__(self):
                super().__init__(select_queue=[[], []])  # 页面 select×2 正常
                self.calls = 0

            async def execute(self, stmt):
                self.calls += 1
                if self.calls > 2:  # links 标题 select 起炸
                    raise RuntimeError("db down")
                return await super().execute(stmt)

        db = FlakyDB()
        compiler = WikiCompiler()
        result = await compiler.persist_pages(
            db, "kb-1", "doc-1",
            [CompiledPage(title="页面A", page_type="entity", content="内容")],
        )
        assert result.pages_created == 1  # links 失败不影响编译产物
        assert vector.indexed  # 向量入库仍执行


# ----------------------------------------------------------------------
# 编排
# ----------------------------------------------------------------------
class TestCompileDocument:
    async def test_orchestrates_load_compile_persist(self, monkeypatch, patched_services):
        compiler = WikiCompiler()
        calls = []

        async def fake_load(db, kb_id):
            calls.append("load")
            return []

        async def fake_compile(chunks, existing):
            calls.append("compile")
            return [CompiledPage(title="T", page_type="topic", content="c")]

        async def fake_persist(db, kb_id, doc_id, compiled):
            calls.append("persist")
            return WikiCompileResult(pages_created=1)

        monkeypatch.setattr(compiler, "_load_existing_pages", fake_load)
        monkeypatch.setattr(compiler, "compile_pages", fake_compile)
        monkeypatch.setattr(compiler, "persist_pages", fake_persist)

        result = await compiler.compile_document(FakeDB([]), "kb-1", "doc-1", FakeChunks.of("x"))
        assert calls == ["load", "compile", "persist"]
        assert result.pages_created == 1

    async def test_no_candidates_skips_persist(self, monkeypatch, patched_services):
        compiler = WikiCompiler()

        async def fake_load(db, kb_id):
            return []

        async def fake_compile(chunks, existing):
            return []

        async def fake_persist(*a, **k):  # pragma: no cover - 不应被调用
            raise AssertionError("不应持久化")

        monkeypatch.setattr(compiler, "_load_existing_pages", fake_load)
        monkeypatch.setattr(compiler, "compile_pages", fake_compile)
        monkeypatch.setattr(compiler, "persist_pages", fake_persist)

        result = await compiler.compile_document(FakeDB([]), "kb-1", "doc-1", FakeChunks.of("x"))
        assert result == WikiCompileResult()


# ----------------------------------------------------------------------
# 诊断探针（Phase 2）：事实保留率
# ----------------------------------------------------------------------
def _probe_page(content: str, facts: List[str]) -> CompiledPage:
    return CompiledPage(title="T", page_type="topic", content=content, key_facts=facts)


class TestFactRetention:
    async def test_disabled_returns_none_without_embeddings(self, monkeypatch):
        monkeypatch.setattr(settings.wiki_compile, "WIKI_DIAGNOSTIC_PROBES", False)
        compiler = WikiCompiler()

        class AssertNotUsed:
            async def aembed_documents(self, texts):
                raise AssertionError("探针关闭时不得调用 embedding")

        monkeypatch.setattr(
            compiler, "_get_embeddings",
            lambda: AssertNotUsed(),
        )
        result = await compiler._check_fact_retention([_probe_page("正文", ["事实"])])
        assert result is None

    async def test_no_facts_returns_none(self, monkeypatch):
        monkeypatch.setattr(settings.wiki_compile, "WIKI_DIAGNOSTIC_PROBES", True)
        compiler = WikiCompiler()
        assert await compiler._check_fact_retention([_probe_page("正文", [])]) is None

    async def test_enabled_computes_retention_rate(self, monkeypatch):
        monkeypatch.setattr(settings.wiki_compile, "WIKI_DIAGNOSTIC_PROBES", True)

        class FakeEmbeddings:
            async def aembed_documents(self, texts):
                table = {
                    "事实甲": [1.0, 0.0],
                    "这是事实甲的句子。": [1.0, 0.0],
                    "无关句。": [0.0, 1.0],
                }
                return [table[t] for t in texts]

        compiler = WikiCompiler()
        monkeypatch.setattr(compiler, "_get_embeddings", lambda: FakeEmbeddings())
        page = _probe_page("这是事实甲的句子。\n无关句。", ["事实甲"])
        assert await compiler._check_fact_retention([page]) == {"T": (1.0, [])}

    async def test_enabled_low_rate(self, monkeypatch):
        monkeypatch.setattr(settings.wiki_compile, "WIKI_DIAGNOSTIC_PROBES", True)

        class FakeEmbeddings:
            async def aembed_documents(self, texts):
                # 事实向量与所有句子向量正交 → 保留率 0
                table = {"事实甲": [1.0, 0.0], "句子一。": [0.0, 1.0], "句子二。": [0.0, 1.0]}
                return [table[t] for t in texts]

        compiler = WikiCompiler()
        monkeypatch.setattr(compiler, "_get_embeddings", lambda: FakeEmbeddings())
        page = _probe_page("句子一。句子二。", ["事实甲"])
        assert await compiler._check_fact_retention([page]) == {"T": (0.0, ["事实甲"])}

    async def test_embedding_error_returns_none(self, monkeypatch):
        monkeypatch.setattr(settings.wiki_compile, "WIKI_DIAGNOSTIC_PROBES", True)

        class BoomEmbeddings:
            async def aembed_documents(self, texts):
                raise ConnectionError("ollama down")

        compiler = WikiCompiler()
        monkeypatch.setattr(compiler, "_get_embeddings", lambda: BoomEmbeddings())
        assert await compiler._check_fact_retention([_probe_page("正文", ["事实"])]) is None


# ----------------------------------------------------------------------
# P4 迭代精炼：弱页重生成闭环
# ----------------------------------------------------------------------
class OrthogonalEmbeddings:
    """事实向量与句子向量正交 → 保留率恒 0（恒为弱页）。"""

    async def aembed_documents(self, texts):
        return [[1.0, 0.0] if t == "关键事实" else [0.0, 1.0] for t in texts]


def _compile_doc_with_pages(compiler, monkeypatch, pages):
    """mock compile_pages/persist_pages，返回 captured dict 收集 persist 入参。"""
    captured = {}

    async def fake_compile(chunks, existing):
        return pages

    async def fake_persist(db, kb_id, doc_id, compiled):
        captured["compiled"] = list(compiled)
        return WikiCompileResult(pages_created=len(compiled))

    monkeypatch.setattr(compiler, "compile_pages", fake_compile)
    monkeypatch.setattr(compiler, "persist_pages", fake_persist)
    return captured


class TestRefinement:
    async def test_disabled_skips_probe_entirely(self, monkeypatch, patched_services):
        """REFINEMENT=0 且 PROBES=False：compile_document 不触碰探针（行为与 Phase 1/2 一致）。"""
        monkeypatch.setattr(settings.wiki_compile, "WIKI_DIAGNOSTIC_PROBES", False)
        monkeypatch.setattr(settings.wiki_compile, "WIKI_COMPILE_REFINEMENT_ITERATIONS", 0)
        compiler = WikiCompiler()

        async def boom(compiled):
            raise AssertionError("探针不应被调用")

        monkeypatch.setattr(compiler, "_check_fact_retention", boom)
        page = CompiledPage(title="T", page_type="topic", content="c", key_facts=["f"])
        captured = _compile_doc_with_pages(compiler, monkeypatch, [page])

        result = await compiler.compile_document(FakeDB([]), "kb-1", "doc-1", FakeChunks.of("x"))
        assert result.fact_retention_rate is None
        assert captured["compiled"][0].content == "c"

    async def test_weak_page_regenerated_with_missing_facts(self, monkeypatch, patched_services):
        """弱页被重生成，prompt 注入缺失事实，persist 收到新内容。"""
        monkeypatch.setattr(settings.wiki_compile, "WIKI_DIAGNOSTIC_PROBES", False)
        monkeypatch.setattr(settings.wiki_compile, "WIKI_COMPILE_REFINEMENT_ITERATIONS", 2)
        compiler = make_compiler(["# T\n补充缺失事实后的正文。"], monkeypatch)
        monkeypatch.setattr(compiler, "_get_embeddings", lambda: OrthogonalEmbeddings())

        page = CompiledPage(title="T", page_type="topic", content="旧正文。", key_facts=["关键事实"])
        captured = _compile_doc_with_pages(compiler, monkeypatch, [page])

        result = await compiler.compile_document(FakeDB([]), "kb-1", "doc-1", FakeChunks.of("x"))
        assert captured["compiled"][0].content == "# T\n补充缺失事实后的正文。"
        assert "关键事实" in compiler._llm.prompts[0]
        assert result.pages_created == 1

    async def test_strong_pages_untouched(self, monkeypatch, patched_services):
        """达标页（保留率 ≥ 0.9）不触发重生成。"""
        monkeypatch.setattr(settings.wiki_compile, "WIKI_DIAGNOSTIC_PROBES", True)
        monkeypatch.setattr(settings.wiki_compile, "WIKI_COMPILE_REFINEMENT_ITERATIONS", 1)
        compiler = WikiCompiler()

        class MatchingEmbeddings:
            async def aembed_documents(self, texts):
                return [[1.0, 0.0] for _ in texts]  # 事实与句子同向 → 全保留

        monkeypatch.setattr(compiler, "_get_embeddings", lambda: MatchingEmbeddings())

        async def boom(page, missing):
            raise AssertionError("达标页不应重生成")

        monkeypatch.setattr(compiler, "refine_pages", boom)
        page = CompiledPage(title="T", page_type="topic", content="事实已保留。", key_facts=["事实已保留"])
        captured = _compile_doc_with_pages(compiler, monkeypatch, [page])

        result = await compiler.compile_document(FakeDB([]), "kb-1", "doc-1", FakeChunks.of("x"))
        assert captured["compiled"][0].content == "事实已保留。"
        assert result.fact_retention_rate == 1.0  # 精炼开启时探针自动启用并记录

    async def test_iteration_cap(self, monkeypatch, patched_services):
        """弱页永不达标时重生成次数 == 配置轮次，不无限循环。"""
        monkeypatch.setattr(settings.wiki_compile, "WIKI_DIAGNOSTIC_PROBES", False)
        monkeypatch.setattr(settings.wiki_compile, "WIKI_COMPILE_REFINEMENT_ITERATIONS", 3)
        compiler = make_compiler(["新1", "新2", "新3"], monkeypatch)
        monkeypatch.setattr(compiler, "_get_embeddings", lambda: OrthogonalEmbeddings())

        page = CompiledPage(title="T", page_type="topic", content="旧正文。", key_facts=["关键事实"])
        captured = _compile_doc_with_pages(compiler, monkeypatch, [page])

        await compiler.compile_document(FakeDB([]), "kb-1", "doc-1", FakeChunks.of("x"))
        assert len(compiler._llm.prompts) == 3
        assert captured["compiled"][0].content == "新3"

    async def test_refine_failure_keeps_content(self, monkeypatch, patched_services):
        """精炼抛错：该页保留当前内容，persist 正常，不阻断编译。"""
        monkeypatch.setattr(settings.wiki_compile, "WIKI_DIAGNOSTIC_PROBES", False)
        monkeypatch.setattr(settings.wiki_compile, "WIKI_COMPILE_REFINEMENT_ITERATIONS", 2)
        compiler = make_compiler([], monkeypatch)  # FakeLLM 无响应 → ainvoke 必抛
        monkeypatch.setattr(compiler, "_get_embeddings", lambda: OrthogonalEmbeddings())

        page = CompiledPage(title="T", page_type="topic", content="旧正文。", key_facts=["关键事实"])
        captured = _compile_doc_with_pages(compiler, monkeypatch, [page])

        await compiler.compile_document(FakeDB([]), "kb-1", "doc-1", FakeChunks.of("x"))
        assert captured["compiled"][0].content == "旧正文。"

    async def test_empty_refine_output_keeps_content(self, monkeypatch, patched_services):
        """精炼输出为空（视为失败）：保留原内容。"""
        monkeypatch.setattr(settings.wiki_compile, "WIKI_DIAGNOSTIC_PROBES", False)
        monkeypatch.setattr(settings.wiki_compile, "WIKI_COMPILE_REFINEMENT_ITERATIONS", 1)
        compiler = make_compiler(["   "], monkeypatch)
        monkeypatch.setattr(compiler, "_get_embeddings", lambda: OrthogonalEmbeddings())

        page = CompiledPage(title="T", page_type="topic", content="旧正文。", key_facts=["关键事实"])
        captured = _compile_doc_with_pages(compiler, monkeypatch, [page])

        await compiler.compile_document(FakeDB([]), "kb-1", "doc-1", FakeChunks.of("x"))
        assert captured["compiled"][0].content == "旧正文。"


class TestAggregateRetention:
    def test_none_when_not_probed(self):
        assert WikiCompiler._aggregate_retention(None, []) is None
        assert WikiCompiler._aggregate_retention({}, [_probe_page("c", ["f"])]) is None

    def test_mixed_pages_weighted_by_fact_count(self):
        pages = [
            CompiledPage(title="A", page_type="topic", content="c", key_facts=["a1", "a2"]),
            CompiledPage(title="B", page_type="topic", content="c", key_facts=["b1", "b2"]),
        ]
        probe = {"A": (1.0, []), "B": (0.5, ["b2"])}
        assert WikiCompiler._aggregate_retention(probe, pages) == pytest.approx(0.75)

    def test_pages_without_facts_excluded(self):
        pages = [
            CompiledPage(title="A", page_type="topic", content="c", key_facts=["a1"]),
            CompiledPage(title="B", page_type="topic", content="c", key_facts=[]),
        ]
        probe = {"A": (1.0, [])}
        assert WikiCompiler._aggregate_retention(probe, pages) == 1.0


# ----------------------------------------------------------------------
# P4 编译 Prometheus 指标埋点（上传管线阶段 7 三出口）
# ----------------------------------------------------------------------
class TestWikiCompileMetrics:
    async def test_ok_path_records_metrics(self, patched_pipeline, monkeypatch):
        doc = patched_pipeline
        monkeypatch.setattr(settings.wiki_compile, "WIKI_COMPILE_ENABLED", True)
        # P5 去抖默认 20s 会把编译移出上传管线，管线埋点测试固定走立即编译分支
        monkeypatch.setattr(settings.wiki_compile, "WIKI_COMPILE_DEBOUNCE_SECONDS", 0)
        recorded = []
        monkeypatch.setattr(
            "src.middleware.prometheus.record_wiki_compile", lambda **kw: recorded.append(kw)
        )

        class OkCompiler:
            async def compile_document(self, *a, **k):
                return WikiCompileResult(pages_created=2, pages_updated=1, fact_retention_rate=0.95)

        monkeypatch.setattr("src.services.wiki_compiler.WikiCompiler", OkCompiler)

        from src.api.document import process_document_async

        await process_document_async(str(doc.id), "f.pdf", "kb-1", "task-1", None, None)

        assert len(recorded) == 1
        assert recorded[0]["result"] == "ok"
        assert recorded[0]["pages_created"] == 2
        assert recorded[0]["pages_updated"] == 1
        assert recorded[0]["fact_retention"] == 0.95
        assert recorded[0]["duration"] >= 0

    async def test_failed_path_records_metrics(self, patched_pipeline, monkeypatch):
        doc = patched_pipeline
        monkeypatch.setattr(settings.wiki_compile, "WIKI_COMPILE_ENABLED", True)
        # P5 去抖默认 20s 会把编译移出上传管线，管线埋点测试固定走立即编译分支
        monkeypatch.setattr(settings.wiki_compile, "WIKI_COMPILE_DEBOUNCE_SECONDS", 0)
        recorded = []
        monkeypatch.setattr(
            "src.middleware.prometheus.record_wiki_compile", lambda **kw: recorded.append(kw)
        )

        class BoomCompiler:
            async def compile_document(self, *a, **k):
                raise RuntimeError("编译炸了")

        monkeypatch.setattr("src.services.wiki_compiler.WikiCompiler", BoomCompiler)

        from src.api.document import process_document_async

        await process_document_async(str(doc.id), "f.pdf", "kb-1", "task-1", None, None)

        assert doc.processing_status == "completed"  # 不阻断上传
        assert recorded == [{"result": "failed"}]

    async def test_timeout_path_records_metrics(self, patched_pipeline, monkeypatch):
        doc = patched_pipeline
        monkeypatch.setattr(settings.wiki_compile, "WIKI_COMPILE_ENABLED", True)
        # P5 去抖默认 20s 会把编译移出上传管线，管线埋点测试固定走立即编译分支
        monkeypatch.setattr(settings.wiki_compile, "WIKI_COMPILE_DEBOUNCE_SECONDS", 0)
        recorded = []
        monkeypatch.setattr(
            "src.middleware.prometheus.record_wiki_compile", lambda **kw: recorded.append(kw)
        )

        class SlowCompiler:
            async def compile_document(self, *a, **k):
                raise asyncio.TimeoutError()

        monkeypatch.setattr("src.services.wiki_compiler.WikiCompiler", SlowCompiler)

        from src.api.document import process_document_async

        await process_document_async(str(doc.id), "f.pdf", "kb-1", "task-1", None, None)

        assert doc.processing_status == "completed"
        assert len(recorded) == 1
        assert recorded[0]["result"] == "timeout"
        assert recorded[0]["duration"] >= 0

    def test_helper_skips_none_fields(self):
        from src.middleware.prometheus import record_wiki_compile

        # duration/fact_retention 为 None 不观察 Histogram（不抛错即通过）
        record_wiki_compile(result="ok", pages_created=1, pages_updated=2)


# ----------------------------------------------------------------------
# 按 KB 串行化（Phase 2）
# ----------------------------------------------------------------------
class TestKbLock:
    def test_same_kb_same_lock(self):
        from src.services.wiki_lock import get_kb_lock

        assert get_kb_lock("kb-a") is get_kb_lock("kb-a")
        assert get_kb_lock("kb-a") is not get_kb_lock("kb-b")

    async def test_compile_document_holds_lock(self, monkeypatch):
        from src.services.wiki_lock import get_kb_lock

        compiler = WikiCompiler()
        lock = get_kb_lock("kb-lock-1")
        observed = []

        async def fake_load(db, kb_id):
            observed.append(lock.locked())
            return []

        monkeypatch.setattr(compiler, "_load_existing_pages", fake_load)

        async def fake_compile(chunks, existing):
            return []

        monkeypatch.setattr(compiler, "compile_pages", fake_compile)

        await compiler.compile_document(FakeDB([]), "kb-lock-1", "doc-1", FakeChunks.of("x"))
        assert observed == [True]      # 编译期间锁被持有
        assert not lock.locked()       # 结束后释放


# ----------------------------------------------------------------------
# 管线挂载（document.py 阶段 7）：失败不阻断上传（验收标准 #4）
# ----------------------------------------------------------------------
class FakeDoc:
    def __init__(self):
        self.id = uuid.uuid4()
        self.filename = "测试文档.pdf"
        self.status = "processing"
        self.processing_status = "processing"
        self.processing_message = ""
        self.processing_progress = 0
        self.chunks_count = 0
        self.document_type = None
        self.document_type_label = None
        self.domain = None
        self.domain_label = None
        self.topics = []
        self.quality_score = 0
        self.quality_grade = ""
        self.quality_details = {}
        self.summary = ""


@pytest.fixture
def patched_pipeline(monkeypatch):
    """mock process_document_async 的全部外部依赖。"""
    doc = FakeDoc()

    class FakeCtxDB(FakeDB):
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    db = FakeCtxDB(select_queue=[[doc], [doc]])  # 正常路径 doc 查询 + 异常兜底查询

    monkeypatch.setattr("src.api.document.async_session_maker", lambda: db)
    monkeypatch.setattr(
        "src.api.document.process_document",
        lambda *a, **k: [SimpleNamespace(page_content="RAG 内容", metadata={})],
    )

    async def fake_get_instance():
        return FakeVectorStore()

    monkeypatch.setattr("src.api.document.VectorStoreManager.get_instance", fake_get_instance)
    monkeypatch.setattr(
        "src.api.document.DocumentAnalyzer.analyze_document_content",
        staticmethod(lambda *a: ("technical", [], "it")),
    )
    monkeypatch.setattr(
        "src.api.document.DocumentAnalyzer.evaluate_quality",
        staticmethod(lambda *a: {"overall_score": 80, "overall_grade": "B"}),
    )

    async def fake_llm_classify(*a):
        return None

    monkeypatch.setattr("src.api.document.classify_document_with_llm", fake_llm_classify)
    monkeypatch.setattr("src.api.document.evaluate_quality_with_llm", fake_llm_classify)

    async def noop(*a, **k):
        return None

    for name in (
        "notify_task_progress",
        "notify_task_completed",
        "notify_doc_list_changed",
        "invalidate_kb_list_cache",
    ):
        monkeypatch.setattr(f"src.api.document.{name}", noop)
    monkeypatch.setattr("src.api.document.update_upload_progress", lambda *a, **k: None)
    return doc


class TestPipelineStageMount:
    async def test_compiler_failure_does_not_block_upload(self, patched_pipeline, monkeypatch):
        doc = patched_pipeline
        monkeypatch.setattr(settings.wiki_compile, "WIKI_COMPILE_ENABLED", True)
        # P5 去抖默认 20s 会把编译移出上传管线，管线埋点测试固定走立即编译分支
        monkeypatch.setattr(settings.wiki_compile, "WIKI_COMPILE_DEBOUNCE_SECONDS", 0)

        class BoomCompiler:
            async def compile_document(self, *a, **k):
                raise RuntimeError("编译炸了")

        monkeypatch.setattr("src.services.wiki_compiler.WikiCompiler", BoomCompiler)

        from src.api.document import process_document_async

        await process_document_async(str(doc.id), "f.pdf", "kb-1", "task-1", None, None)

        assert doc.processing_status == "completed"
        assert doc.processing_progress == 100

    async def test_compiler_success_reports_progress(self, patched_pipeline, monkeypatch):
        doc = patched_pipeline
        monkeypatch.setattr(settings.wiki_compile, "WIKI_COMPILE_ENABLED", True)
        # P5 去抖默认 20s 会把编译移出上传管线，管线埋点测试固定走立即编译分支
        monkeypatch.setattr(settings.wiki_compile, "WIKI_COMPILE_DEBOUNCE_SECONDS", 0)
        progress_messages = []

        async def fake_notify(task_id, progress, message=None, data=None):
            progress_messages.append((progress, message))

        monkeypatch.setattr("src.api.document.notify_task_progress", fake_notify)

        class OkCompiler:
            async def compile_document(self, *a, **k):
                return WikiCompileResult(pages_created=2, pages_updated=1, chunks_indexed=5)

        monkeypatch.setattr("src.services.wiki_compiler.WikiCompiler", OkCompiler)

        from src.api.document import process_document_async

        await process_document_async(str(doc.id), "f.pdf", "kb-1", "task-1", None, None)

        assert doc.processing_status == "completed"
        assert any(p == 95 and "Wiki" in (m or "") for p, m in progress_messages)
        assert any(p == 98 and "新建 2 页" in (m or "") for p, m in progress_messages)

    async def test_disabled_skips_compile_stage(self, patched_pipeline, monkeypatch):
        doc = patched_pipeline
        monkeypatch.setattr(settings.wiki_compile, "WIKI_COMPILE_ENABLED", False)
        # 开关关闭时 WikiCompiler 不应被构造——若被调用会触发 import 内的真实类并失败

        class AssertNotUsed:
            def __init__(self, *a, **k):
                raise AssertionError("开关关闭时不得实例化编译器")

        monkeypatch.setattr("src.services.wiki_compiler.WikiCompiler", AssertNotUsed)

        from src.api.document import process_document_async

        await process_document_async(str(doc.id), "f.pdf", "kb-1", "task-1", None, None)
        assert doc.processing_status == "completed"


# ----------------------------------------------------------------------
# P5：doc_ids 多值、regenerate_page、矛盾抽查
# ----------------------------------------------------------------------
class TestNormalizeDocIds:
    def test_single_str(self):
        assert normalize_doc_ids("doc-1") == ["doc-1"]

    def test_list_dedup_preserves_order(self):
        assert normalize_doc_ids(["a", "b", "a"]) == ["a", "b"]

    def test_non_str_items_coerced(self):
        assert normalize_doc_ids([uuid.UUID(int=1), "b"]) == [
            str(uuid.UUID(int=1)), "b"
        ]


class TestBuildMaterial:
    def test_concatenates_and_truncates(self):
        chunks = FakeChunks.of("A" * 100, "B" * 100)
        assert build_material(chunks, 150) == ("A" * 100 + "\n\n" + "B" * 100)[:150]

    def test_skips_empty(self):
        chunks = FakeChunks.of("", "内容")
        assert build_material(chunks, 1000) == "内容"


class TestPersistMultiDocIds:
    async def test_new_page_writes_all_doc_ids(self, patched_services):
        db = FakeDB(select_queue=[[], [], []])
        compiler = WikiCompiler()
        pages = [CompiledPage(title="页A", page_type="entity", content="# A")]
        result = await compiler.persist_pages(db, "kb-1", ["d1", "d2"], pages)
        assert result.pages_created == 1
        assert db.added[0].source_doc_ids == ["d1", "d2"]

    async def test_existing_page_appends_missing_doc_ids(self, patched_services):
        existing = SimpleNamespace(
            id="p1", kb_id="kb-1", page_type="entity", title="页A",
            content_path="wiki/kb-1/p1.md", source_doc_ids=["d0"], revision=1,
            status="active", owner_id=None,
        )
        db = FakeDB(select_queue=[[existing], [], []])
        compiler = WikiCompiler()
        await compiler.persist_pages(
            db, "kb-1", ["d0", "d1"],
            [CompiledPage(title="页A", page_type="entity", content="更新")],
        )
        assert existing.source_doc_ids == ["d0", "d1"]
        assert existing.revision == 2


class TestRegeneratePage:
    async def test_generate_from_remaining_material(self, monkeypatch):
        compiler = make_compiler(["# 重写页\n\n正文"], monkeypatch)
        content = await compiler.regenerate_page("主题", "entity", "剩余材料内容")
        assert content.startswith("# 重写页")
        assert "已删除" in compiler._llm.prompts[0]
        assert "剩余材料" in compiler._llm.prompts[0]

    async def test_empty_output_raises(self, monkeypatch):
        compiler = make_compiler(["  "], monkeypatch)
        with pytest.raises(ValueError, match="重写输出为空"):
            await compiler.regenerate_page("T", "topic", "材料")

    async def test_material_truncated_by_config(self, monkeypatch):
        monkeypatch.setattr(settings.wiki_compile, "WIKI_REWRITE_MATERIAL_CHARS", 50)
        compiler = make_compiler(["内容"], monkeypatch)
        await compiler.regenerate_page("T", "topic", "X" * 200)
        prompt = compiler._llm.prompts[0]
        assert "X" * 50 in prompt     # 截断后保留了上限内的材料
        assert "X" * 51 not in prompt  # 超上限部分未进入 prompt


class TestContradictionCheck:
    async def test_disabled_skips_check_in_pipeline(self, monkeypatch, patched_services):
        """开关关闭时 compile_document 不得调用矛盾抽查。"""
        monkeypatch.setattr(settings.wiki_compile, "WIKI_CONTRADICTION_CHECK", False)
        called = []

        async def fake_check(self, compiled, material):
            called.append("check")
        monkeypatch.setattr(WikiCompiler, "_check_contradictions", fake_check)

        compiler = WikiCompiler()

        async def fake_load(db, kb):
            return []

        async def fake_compile(chunks, existing):
            return [CompiledPage(title="T", page_type="topic", content="c")]

        monkeypatch.setattr(compiler, "_load_existing_pages", fake_load)
        monkeypatch.setattr(compiler, "compile_pages", fake_compile)

        async def fake_persist(db, kb, doc_ids, compiled):
            return WikiCompileResult(pages_created=1)
        monkeypatch.setattr(compiler, "persist_pages", fake_persist)

        await compiler.compile_document(FakeDB([]), "kb-1", "doc-1", FakeChunks.of("x"))
        assert called == []

    async def test_new_pages_skipped(self, monkeypatch):
        monkeypatch.setattr(settings.wiki_compile, "WIKI_CONTRADICTION_CHECK", True)
        compiler = WikiCompiler()
        calls = []

        class Fake:
            async def ainvoke(self, p):
                calls.append(p)
                return SimpleNamespace(content='{"contradictions": []}')
        monkeypatch.setattr(compiler, "_get_llm", lambda: Fake())
        # matched=None 的新页不抽查
        await compiler._check_contradictions(
            [CompiledPage(title="T", page_type="topic", content="c", matched=None)], "材料"
        )
        assert calls == []

    async def test_update_page_reports_contradictions(self, monkeypatch):
        monkeypatch.setattr(settings.wiki_compile, "WIKI_CONTRADICTION_CHECK", True)
        recorded = []
        monkeypatch.setattr(
            "src.middleware.prometheus.record_wiki_contradictions",
            lambda count=1: recorded.append(count),
        )
        compiler = WikiCompiler()
        existing = ExistingPage(page_id="p1", title="T", page_type="topic", content="旧内容")
        monkeypatch.setattr(compiler, "_get_llm", lambda: FakeLLM(
            ['{"contradictions": ["矛盾A", "矛盾B"]}']
        ))
        await compiler._check_contradictions(
            [CompiledPage(title="T", page_type="topic", content="新内容", matched=existing)],
            "材料",
        )
        assert recorded == [2]

    async def test_parse_failure_swallowed(self, monkeypatch):
        monkeypatch.setattr(settings.wiki_compile, "WIKI_CONTRADICTION_CHECK", True)
        compiler = WikiCompiler()
        existing = ExistingPage(page_id="p1", title="T", page_type="topic", content="旧")
        monkeypatch.setattr(compiler, "_get_llm", lambda: FakeLLM(["不是JSON"]))
        # 解析失败不抛错
        await compiler._check_contradictions(
            [CompiledPage(title="T", page_type="topic", content="新", matched=existing)],
            "材料",
        )


class TestCompileDocumentWithContradictionCheck:
    async def test_check_runs_before_persist(self, monkeypatch, patched_services):
        """开启矛盾抽查时，compile_document 在 persist 前调用 _check_contradictions。"""
        monkeypatch.setattr(settings.wiki_compile, "WIKI_CONTRADICTION_CHECK", True)
        check_called = []

        async def fake_check(self, compiled, material):
            check_called.append("check")
        monkeypatch.setattr(WikiCompiler, "_check_contradictions", fake_check)

        compiler = WikiCompiler()

        async def fake_load(db, kb):
            return []

        async def fake_compile(chunks, existing):
            return [CompiledPage(title="T", page_type="topic", content="c")]

        monkeypatch.setattr(compiler, "_load_existing_pages", fake_load)
        monkeypatch.setattr(compiler, "compile_pages", fake_compile)

        async def fake_persist(db, kb, doc_ids, compiled):
            return WikiCompileResult(pages_created=1)
        monkeypatch.setattr(compiler, "persist_pages", fake_persist)

        await compiler.compile_document(FakeDB([]), "kb-1", "doc-1", FakeChunks.of("x"))
        assert check_called == ["check"]
