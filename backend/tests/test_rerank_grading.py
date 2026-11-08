"""重排序失败的分级：单次抖动 vs 持续失效。

为什么单独测：重排序失败是「静默降级」——问答照常返回，只是排序变差。
如果分级永远是 recoverable（warning、无堆栈），模型名配错这种**不可自愈**的
故障会一直沉底，而用户只会觉得「搜索结果不太对」。

判据两条：
1. 前两次失败 = recoverable（降级仍在工作，抖动不值得报警）；
2. 连续第 3 次 = reportable，并带 consecutive_failures，便于告警聚合。
另外失败时 rerank_score 必须回退成 rrf_score，**不能是 0**——0 分会被
KB_RERANK_MIN_SCORE 过滤掉，整批结果消失，表现成「知识库没有相关内容」。
"""

import asyncio
import logging

import pytest

from src.services.hybrid_search import KBReranker
from src.services.pipeline.retrieval import RetrievalMixin


class _BoomReranker:
    """打分会抛异常的 reranker（模拟模型不存在/服务没起）。"""

    async def predict(self, pairs):
        raise RuntimeError("model 'no-such-reranker' not found")


class _OkReranker:
    async def predict(self, pairs):
        return [0.8 for _ in pairs]


@pytest.fixture(autouse=True)
def _isolate_reranker_state():
    saved = (
        KBReranker._model_loaded,
        KBReranker._ollama_reranker,
        KBReranker._consecutive_failures,
    )
    KBReranker._model_loaded = True
    KBReranker._ollama_reranker = _BoomReranker()
    KBReranker._consecutive_failures = 0
    yield
    (
        KBReranker._model_loaded,
        KBReranker._ollama_reranker,
        KBReranker._consecutive_failures,
    ) = saved


def _rerank_once():
    reranker = KBReranker(model_name="no-such-reranker", provider="ollama")
    return asyncio.run(
        reranker.rerank("问题", [{"content": "候选内容", "rrf_score": 0.42}], top_k=1)
    )


def _rerank_messages(caplog) -> list[str]:
    return [
        rec.getMessage()
        for rec in caplog.records
        if "action=hybrid_search.rerank" in rec.getMessage()
    ]


class TestRerankFailureGrading:
    def test_first_two_failures_stay_recoverable(self, caplog):
        with caplog.at_level(logging.WARNING, logger="rag_system"):
            _rerank_once()
            _rerank_once()

        messages = _rerank_messages(caplog)
        assert len(messages) == 2
        assert all("severity=recoverable" in m for m in messages), messages

    def test_third_consecutive_failure_escalates_to_reportable(self, caplog):
        with caplog.at_level(logging.WARNING, logger="rag_system"):
            for _ in range(3):
                _rerank_once()

        messages = _rerank_messages(caplog)
        assert len(messages) == 3
        assert "severity=recoverable" in messages[1]
        assert "severity=reportable" in messages[2]
        assert "consecutive_failures=3" in messages[2]

    def test_failed_scoring_falls_back_to_rrf_not_zero(self, caplog):
        """打分失败不能当 0 分：0 分会被阈值滤空 → 「没有相关内容」的假象。"""
        with caplog.at_level(logging.WARNING, logger="rag_system"):
            results = _rerank_once()

        assert results, "失败时也必须返回候选，而不是空列表"
        assert results[0]["rerank_score"] == pytest.approx(0.42)
        assert results[0]["rerank_fallback"] is True, "回退 RRF 必须打标，下游才知道这不是相关性分"

    def test_success_resets_consecutive_counter(self, caplog):
        with caplog.at_level(logging.WARNING, logger="rag_system"):
            _rerank_once()
            _rerank_once()
            assert KBReranker._consecutive_failures == 2

            KBReranker._ollama_reranker = _OkReranker()
            _rerank_once()
            assert KBReranker._consecutive_failures == 0

            # 复原后重新计数：下一次失败应回到 recoverable，而不是立刻报 error
            KBReranker._ollama_reranker = _BoomReranker()
            _rerank_once()

        assert "severity=recoverable" in _rerank_messages(caplog)[-1]


class _RelevanceStage(RetrievalMixin):
    """只借 _calculate_relevance 用的最小宿主（真实宿主是 RAGChain）。"""

    def __init__(self):
        self.sentence_transformer = None
        self.similarity_util = None
        self.similarity_threshold = 0.5


class TestRerankFallbackDoesNotLookLikeNoContent:
    """重排失败 → RRF 回退分被当成相关性分 → 候选全被滤掉 → 「没有相关内容」。

    这是 2026-11-08 故障演练抓到的真实链路：日志分级是对的（reportable），
    用户侧却看到「知识库里没有找到相关内容」——基础设施降级被说成了内容缺失。
    """

    def _docs(self, **metadata):
        from langchain_core.documents import Document

        return [Document(page_content="片段内容", metadata={"rerank_score": 0.016, **metadata})]

    def test_fallback_docs_survive_relevance_gate(self, monkeypatch):
        stage = _RelevanceStage()
        # 相似度模型不可用时属于「测不出来」，按既有约定 fail-open（不丢知识库结果）
        monkeypatch.setattr(stage, "_init_similarity_model", lambda: None)

        _score, has_relevant = asyncio.run(
            stage._calculate_relevance("问题", self._docs(rerank_fallback=True))
        )

        assert has_relevant is True, "回退 RRF 的结果不能被相关性门禁当成「不相关」清空"

    def test_real_rerank_score_still_gated(self, monkeypatch):
        """对照：真正打过分的低分候选仍应被门禁拦下（修完不能把门禁一起废掉）。"""
        stage = _RelevanceStage()
        monkeypatch.setattr(stage, "_init_similarity_model", lambda: None)

        _score, has_relevant = asyncio.run(
            stage._calculate_relevance("问题", self._docs())
        )

        assert has_relevant is False
