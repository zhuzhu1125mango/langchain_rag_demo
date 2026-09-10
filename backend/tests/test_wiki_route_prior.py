"""KB 覆盖先验单元测试（P3，§11.1，embedding 全 mock）。

覆盖：融合前验计算（有/无 index 页两态）、负余弦截断、TTL 缓存命中与
revision 失效、失败回退纯 chunk 分。
"""

from types import SimpleNamespace

import pytest

from src.services.wiki_route_prior import WikiRoutePrior, _cosine


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


class FakeMinio:
    def __init__(self, contents):
        self.contents = contents

    async def download_text_async(self, path):
        return self.contents.get(path, "")


class FakeEmbeddings:
    """按文本查表返回向量，记录调用批次。"""

    def __init__(self, table, default=None):
        self.table = table
        self.default = default or [0.0, 1.0]
        self.calls = []

    async def aembed_documents(self, texts):
        self.calls.append(list(texts))
        return [self.table.get(t, self.default) for t in texts]


@pytest.fixture
def patched_env(monkeypatch):
    """patch MinIO 与 model_manager，返回 (minio, embeddings)。"""
    minio = FakeMinio({})
    embeddings = FakeEmbeddings({})

    async def fake_minio_get():
        return minio

    monkeypatch.setattr("src.services.minio_service.MinioService.get_instance", fake_minio_get)

    from src.services import model_manager

    monkeypatch.setattr(model_manager.model_manager, "get_embeddings", lambda: embeddings)
    return minio, embeddings


def index_row(pid, kb_id, revision, content):
    return SimpleNamespace(
        id=pid, kb_id=kb_id, page_type="index", revision=revision,
        status="active", content_path=f"wiki/{kb_id}/{pid}.md",
    ), content


class TestComputePriors:
    async def test_kb_with_index_gets_prior(self, patched_env, monkeypatch):
        minio, embeddings = patched_env
        row, content = index_row("idx-1", "kb-a", 1, "KB 目录：RAG 与向量库")
        minio.contents[row.content_path] = content
        embeddings.table = {
            "什么是 RAG？": [1.0, 0.0],
            "KB 目录：RAG 与向量库": [1.0, 0.0],
        }
        prior = WikiRoutePrior()

        result = await prior.compute_priors(FakeDB([[row]]), "什么是 RAG？", ["kb-a", "kb-b"])

        # 无 index 页的 kb-b 不出现在结果中（保持纯 chunk 分）
        assert set(result.keys()) == {"kb-a"}
        assert result["kb-a"] == pytest.approx(1.0)

    async def test_negative_cosine_clamped_to_zero(self, patched_env):
        minio, embeddings = patched_env
        row, content = index_row("idx-1", "kb-a", 1, "目录")
        minio.contents[row.content_path] = content
        embeddings.table = {"问题": [1.0, 0.0], "目录": [-1.0, 0.0]}
        prior = WikiRoutePrior()

        result = await prior.compute_priors(FakeDB([[row]]), "问题", ["kb-a"])
        assert result["kb-a"] == 0.0

    async def test_empty_content_index_skipped(self, patched_env):
        minio, _ = patched_env
        row, _ = index_row("idx-1", "kb-a", 1, "目录")
        minio.contents[row.content_path] = "  "  # 空白正文
        prior = WikiRoutePrior()

        assert await prior.compute_priors(FakeDB([[row]]), "问题", ["kb-a"]) == {}

    async def test_no_rows_returns_empty(self, patched_env):
        prior = WikiRoutePrior()
        assert await prior.compute_priors(FakeDB([[]]), "问题", ["kb-a"]) == {}


class TestEmbeddingCache:
    async def test_cached_page_not_reembedded(self, patched_env):
        minio, embeddings = patched_env
        row, content = index_row("idx-1", "kb-a", 1, "目录内容")
        minio.contents[row.content_path] = content
        prior = WikiRoutePrior()

        await prior.compute_priors(FakeDB([[row]]), "问题一", ["kb-a"])
        await prior.compute_priors(FakeDB([[row]]), "问题二", ["kb-a"])

        # 第二次只 embed 新问题，目录正文命中 (page_id, revision) 缓存
        assert embeddings.calls[0] == ["问题一", "目录内容"]
        assert embeddings.calls[1] == ["问题二"]

    async def test_revision_bump_invalidates_cache(self, patched_env):
        minio, embeddings = patched_env
        row, content = index_row("idx-1", "kb-a", 1, "目录内容")
        minio.contents[row.content_path] = content
        prior = WikiRoutePrior()

        await prior.compute_priors(FakeDB([[row]]), "问题一", ["kb-a"])
        row.revision = 2
        await prior.compute_priors(FakeDB([[row]]), "问题二", ["kb-a"])

        assert embeddings.calls[1] == ["问题二", "目录内容"]

    async def test_ttl_expiry_reembeds(self, patched_env, monkeypatch):
        from types import SimpleNamespace

        from src.services import wiki_route_prior as mod

        minio, embeddings = patched_env
        row, content = index_row("idx-1", "kb-a", 1, "目录内容")
        minio.contents[row.content_path] = content
        prior = WikiRoutePrior()

        await prior.compute_priors(FakeDB([[row]]), "问题一", ["kb-a"])
        # 时间快进越过 TTL（仅替换服务模块内的 time 引用）
        monkeypatch.setattr(mod, "time", SimpleNamespace(monotonic=lambda: 10 ** 12))
        await prior.compute_priors(FakeDB([[row]]), "问题二", ["kb-a"])
        assert embeddings.calls[1] == ["问题二", "目录内容"]


class TestGetPriorsFallback:
    async def test_empty_kb_ids_short_circuit(self):
        prior = WikiRoutePrior()
        assert await prior.get_priors("问题", []) == {}

    async def test_exception_returns_empty(self, monkeypatch):
        def boom():
            raise RuntimeError("db down")

        monkeypatch.setattr("src.database.async_session_maker", boom)
        prior = WikiRoutePrior()
        assert await prior.get_priors("问题", ["kb-a"]) == {}


class TestCosineLocal:
    def test_matches_semantics(self):
        assert _cosine([1, 0], [1, 0]) == pytest.approx(1.0)
        assert _cosine([1], [1, 2]) == 0.0
        assert _cosine([], []) == 0.0
