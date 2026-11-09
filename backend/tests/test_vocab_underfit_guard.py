"""启动期词表欠拟合告警（2026-11-09）。

事故复盘：词表在 5 条演练文本上拟合，服役于 532 条真实切片。稀疏通道整条失效，
但**没有任何报错**，只是「有些该命中的问题不命中」。这条告警的目的不是修数据，
而是让下一次部署能自己喊出来——用 corpus_size 与集合行数的比值当证据。
"""

import pytest

import src.services.milvus_schema as schema_module
from src.services.milvus_service import MilvusService


def make_service(ef, total, enabled=True):
    svc = MilvusService.__new__(MilvusService)
    svc.bm25_ef = ef
    svc._sparse_enabled = enabled
    svc._has_heading_path = False
    svc._has_source_kind = False

    async def _collection_count(collection_name):
        return total

    svc._collection_count = _collection_count
    return svc


def fake_ef(corpus_size):
    return type("EF", (), {"corpus_size": corpus_size, "idf": {}})()


class TestUnderfittedVocabAlert:
    @pytest.fixture()
    def logs(self, monkeypatch):
        collected = []
        monkeypatch.setattr(
            schema_module, "log_exception", lambda *a, **kw: collected.append(kw)
        )
        return collected

    async def test_alerts_with_evidence(self, logs):
        svc = make_service(fake_ef(5), total=532)
        await svc._warn_if_vocab_underfitted()

        assert len(logs) == 1
        assert logs[0]["action"] == "milvus.bm25_vocab.underfitted"
        assert logs[0]["severity"].name == "REPORTABLE"
        # 证据必须能被检索到：只报「词表太小」没人能判断严重程度
        assert logs[0]["corpus_size"] == 5
        assert logs[0]["collection_rows"] == 532

    async def test_silent_when_vocab_matches_collection(self, logs):
        svc = make_service(fake_ef(520), total=532)
        await svc._warn_if_vocab_underfitted()
        assert logs == []

    async def test_silent_when_sparse_disabled(self, logs):
        svc = make_service(fake_ef(5), total=532, enabled=False)
        await svc._warn_if_vocab_underfitted()
        assert logs == []

    async def test_silent_when_vocab_not_loaded(self, logs):
        svc = make_service(None, total=532)
        await svc._warn_if_vocab_underfitted()
        assert logs == []

    async def test_count_failure_does_not_block_startup(self, logs):
        """启动期的附加检查失败只能记一笔，不能把服务拦在门外。"""
        svc = make_service(fake_ef(5), total=532)

        async def _boom(collection_name):
            raise RuntimeError("milvus unreachable")

        svc._collection_count = _boom
        await svc._warn_if_vocab_underfitted()

        assert len(logs) == 1
        assert logs[0]["action"] == "milvus.bm25_vocab.check"
        assert logs[0]["severity"].name == "RECOVERABLE"
