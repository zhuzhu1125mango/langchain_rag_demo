"""稀疏检索的两个「静默失效点」（2026-11-09）。

为什么这两个点值得单独钉住：它们都不会让请求失败，只会让**混合检索悄悄退化成
dense 单通道**。表现出来是「答得还行，但一些该命中的问题不命中」，没有任何报错，
也不会有人去看日志——这比抛异常难发现得多。

1. **负权重**：Milvus 稀疏向量在 IP 度量下要求分量 ≥ 0，而 BM25 的 IDF 天然会出负数
   （词出现在超过一半文档里）。原实现原样透传 → 整条稀疏查询被拒（`Invalid sparse row`）。
2. **空查询向量**：查询词全部不在词表里时，转换结果是 `[{}]`。`[{}]` 是 **truthy**，
   能通过 `if not query_sparse` 的检查，于是空行被发出去，谁也不知道这一路没参与。
"""

import numpy as np
import pytest

import src.services.milvus_search as search_module
from src.services.milvus_service import MilvusService


def make_service(ef=None) -> MilvusService:
    """未连接 Milvus 的轻量实例（与 test_bm25_vocab_persistence 同一套路）。"""
    svc = MilvusService.__new__(MilvusService)
    svc.bm25_ef = ef
    svc._sparse_enabled = True
    return svc


class _FakeEF:
    """encode_queries 返回固定向量的替身。"""

    def __init__(self, vector, vocab=("备份", "恢复")):
        self._vector = vector
        self.idf_word = list(vocab)

    def encode_queries(self, texts):
        return [np.array(self._vector, dtype=float) for _ in texts]


class TestNegativeWeightsAreClamped:
    def test_negative_and_zero_are_dropped(self):
        """负分量若不截断，整条查询会被 Milvus 拒绝——宁可少一个词，不可整条失败。"""
        svc = make_service()
        out = svc._convert_sparse_embeddings([np.array([0.5, -0.3, 0.0, 1.2])])
        assert out == [{0: 0.5, 3: 1.2}]

    def test_all_negative_becomes_empty(self):
        svc = make_service()
        out = svc._convert_sparse_embeddings([np.array([-0.1, -2.0])])
        assert out == [{}]

    def test_positive_values_unchanged(self):
        """截断不能把正常权重也改掉（防「修一个 bug 引入另一个」）。"""
        svc = make_service()
        out = svc._convert_sparse_embeddings([np.array([1.0986, 0.25])])
        assert out == [{0: 1.0986, 1: 0.25}]


class TestEmptyQueryVectorIsNotSent:
    @pytest.fixture(autouse=True)
    def _reset_logged_flag(self, monkeypatch):
        # 模块级「只报一次」标志：不重置的话第二个用例会永远看不到日志
        monkeypatch.setattr(search_module, "_sparse_empty_logged", False)

    async def test_all_zero_vector_returns_empty_without_calling_milvus(self, monkeypatch):
        """全零向量应该直接返回，不该往 Milvus 发一条注定无效的请求。"""
        called = []
        svc = make_service(_FakeEF([0.0, 0.0]))
        svc.client = type("C", (), {"search": lambda *a, **k: called.append(1)})()

        out = await svc.search_sparse("这些文档是关于什么的？", k=3, kb_ids=["kb-1"])

        assert out == []
        assert called == [], "空向量不应触发 Milvus 请求"

    async def test_logged_once_not_per_request(self, monkeypatch):
        """部署期状态，报一次就够；每条请求刷一遍会把日志冲掉。"""
        logs = []
        monkeypatch.setattr(
            search_module, "log_exception", lambda *a, **kw: logs.append(kw)
        )
        svc = make_service(_FakeEF([0.0, 0.0]))

        await svc.search_sparse("问题一", k=3, kb_ids=["kb-1"])
        await svc.search_sparse("问题二", k=3, kb_ids=["kb-1"])

        assert len(logs) == 1
        assert logs[0]["action"] == "milvus.search.sparse_empty"
        assert logs[0]["vocab_size"] == 2
