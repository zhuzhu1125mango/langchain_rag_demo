"""BM25 词表持久化单元测试（#14）。

验证目标：服务重启后对同一 query 的 sparse 向量与重启前一致；
词表一旦就绪不再重 fit，保证历史 sparse 向量与查询编码空间一致。
"""

import os

import pytest

from src.services.milvus_service import MilvusService

# 首次 fit 使用的语料
CORPUS_A = [
    "机器学习是人工智能的核心技术",
    "向量数据库支持混合检索与重排序",
    "知识库问答系统结合检索与生成",
]
# 模拟重启后插入的全新语料（与 CORPUS_A 无重叠词）
CORPUS_B = [
    "森林防火监测预警平台",
    "港口集装箱调度优化方案",
]


@pytest.fixture
def vocab_dir(tmp_path, monkeypatch):
    """词表持久化目录指向临时路径，测试结束自动清理。"""
    target = str(tmp_path / "bm25")
    monkeypatch.setattr(
        "src.services.milvus_service.settings.milvus.MILVUS_BM25_VOCAB_DIR", target
    )
    return target


def make_service() -> MilvusService:
    """构造未连接 Milvus 的轻量实例，仅测试 BM25 词表管理逻辑。"""
    svc = MilvusService.__new__(MilvusService)
    svc.bm25_ef = None
    svc._sparse_enabled = True
    return svc


def _vocab_file(svc: MilvusService) -> str:
    return svc._bm25_vocab_path()


class TestBm25VocabPersistence:

    @pytest.mark.asyncio
    async def test_fit_persists_vocabulary_file(self, vocab_dir):
        """首次 fit 后词表落盘。"""
        svc = make_service()
        await svc._ensure_bm25_fitted(CORPUS_A)

        assert os.path.exists(_vocab_file(svc))
        assert svc.bm25_ef is not None

    @pytest.mark.asyncio
    async def test_restart_loads_identical_query_encoding(self, vocab_dir):
        """核心场景：模拟重启后（新实例）对同一 query 的 sparse 向量与重启前一致。

        重启后即使传入全新语料插入，也应加载磁盘词表而非重 fit。
        """
        svc_before = make_service()
        await svc_before._ensure_bm25_fitted(CORPUS_A)
        dim_before = svc_before.bm25_ef.dim

        # 模拟重启：全新实例，内存中无词表
        svc_after = make_service()
        await svc_after._ensure_bm25_fitted(CORPUS_B)

        # 未用新语料重 fit：词表维度与重启前一致
        assert svc_after.bm25_ef.dim == dim_before

        q_before = svc_before._convert_sparse_embeddings(
            svc_before.bm25_ef.encode_queries(["混合检索"])
        )
        q_after = svc_after._convert_sparse_embeddings(
            svc_after.bm25_ef.encode_queries(["混合检索"])
        )
        assert q_before == q_after

    @pytest.mark.asyncio
    async def test_already_fitted_skips_refit(self, vocab_dir):
        """词表就绪后再次插入不同语料，不重新 fit（idf 保持不变）。"""
        svc = make_service()
        await svc._ensure_bm25_fitted(CORPUS_A)
        idf_snapshot = dict(svc.bm25_ef.idf)

        await svc._ensure_bm25_fitted(CORPUS_B)

        assert svc.bm25_ef.idf == idf_snapshot

    @pytest.mark.asyncio
    async def test_corrupt_vocab_falls_back_to_fit(self, vocab_dir):
        """词表文件损坏时加载失败并回退为当前语料 fit。"""
        svc = make_service()
        path = _vocab_file(svc)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{corrupted json")

        await svc._ensure_bm25_fitted(CORPUS_A)

        assert svc.bm25_ef is not None
        assert svc.bm25_ef.dim > 0

    @pytest.mark.asyncio
    async def test_sparse_disabled_skips_load(self, vocab_dir):
        """sparse 未启用时不加载词表。"""
        svc = make_service()
        svc._sparse_enabled = False

        assert svc._load_bm25_vocabulary() is False
        assert svc.bm25_ef is None

    @pytest.mark.asyncio
    async def test_new_collection_resets_vocab_file(self, vocab_dir):
        """集合新建时持久化词表被删除（词表与 collection 版本绑定）。"""
        svc = make_service()
        path = _vocab_file(svc)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")
        assert os.path.exists(path)

        svc._remove_bm25_vocabulary()

        assert not os.path.exists(path)
