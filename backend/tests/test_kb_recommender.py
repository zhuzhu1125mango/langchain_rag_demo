"""知识库推荐 + 覆盖先验融合单元测试（P3，§11.1）。

覆盖：融合公式与权重（mock 先验）、无 index 页 KB 保持纯 chunk 分、
权重为 0 时完全跳过先验计算（与现状零差异）。
"""

import pytest

from src.config import settings
from src.services.kb_recommender import KBRecommender, fuse_prior

from langchain_core.documents import Document


def kb_doc(kb_id: str, score: float) -> Document:
    return Document(page_content="内容", metadata={"kb_id": kb_id, "score": score})


class FakeRagChain:
    def __init__(self, docs):
        self.docs = docs

    def _has_vector_store(self):
        return True

    async def _retrieve_documents(self, question, kb_ids=None):
        return self.docs


class TestFusePrior:
    def test_formula_and_weight(self):
        assert fuse_prior(0.2, 0.9, 0.3) == pytest.approx(0.2 * 0.7 + 0.9 * 0.3)

    def test_none_prior_keeps_chunk_score(self):
        assert fuse_prior(0.2, None, 0.3) == 0.2

    def test_zero_weight_keeps_chunk_score(self):
        assert fuse_prior(0.2, 0.9, 0.0) == 0.2


class TestRecommendWithPrior:
    async def test_prior_boosts_ranking(self, monkeypatch):
        monkeypatch.setattr(settings.wiki_compile, "WIKI_ROUTE_PRIOR_WEIGHT", 0.3)
        recommender = KBRecommender(FakeRagChain([
            kb_doc("kb-a", 0.2), kb_doc("kb-a", 0.2),  # chunk 均值 0.2
            kb_doc("kb-b", 0.5),                        # 无 index 页
        ]))

        async def fake_priors(question, kb_ids):
            assert set(kb_ids) == {"kb-a", "kb-b"}
            return {"kb-a": 0.9}  # 仅 kb-a 有 index 页

        monkeypatch.setattr(recommender, "_get_route_priors", fake_priors)

        result = await recommender.recommend_knowledge_bases("问题", top_k=3)

        by_kb = {r["kb_id"]: r for r in result}
        # kb-a：0.2×0.7 + 0.9×0.3 = 0.41；kb-b：纯 chunk 分 0.5
        assert by_kb["kb-a"]["relevance_score"] == pytest.approx(0.41)
        assert by_kb["kb-b"]["relevance_score"] == pytest.approx(0.5)
        assert by_kb["kb-a"]["route_prior"] == pytest.approx(0.9)
        assert by_kb["kb-b"]["route_prior"] is None
        assert by_kb["kb-a"]["chunk_score"] == pytest.approx(0.2)
        assert result[0]["kb_id"] == "kb-b"  # 未被抬升到第一位

    async def test_prior_can_lift_to_first(self, monkeypatch):
        monkeypatch.setattr(settings.wiki_compile, "WIKI_ROUTE_PRIOR_WEIGHT", 0.3)
        recommender = KBRecommender(FakeRagChain([
            kb_doc("kb-a", 0.4),
            kb_doc("kb-b", 0.5),
        ]))

        async def fake_priors(question, kb_ids):
            return {"kb-a": 1.0}  # kb-a = 0.4×0.7 + 1.0×0.3 = 0.58

        monkeypatch.setattr(recommender, "_get_route_priors", fake_priors)

        result = await recommender.recommend_knowledge_bases("问题", top_k=3)
        assert result[0]["kb_id"] == "kb-a"

    async def test_zero_weight_skips_prior_entirely(self, monkeypatch):
        monkeypatch.setattr(settings.wiki_compile, "WIKI_ROUTE_PRIOR_WEIGHT", 0.0)
        recommender = KBRecommender(FakeRagChain([kb_doc("kb-a", 0.5)]))

        result = await recommender.recommend_knowledge_bases("问题", top_k=3)
        assert result[0]["relevance_score"] == pytest.approx(0.5)
        assert result[0]["route_prior"] is None

    async def test_no_priors_falls_back_to_chunk_scores(self, monkeypatch):
        """先验全失败（无 index 页/异常兜底）→ 行为与现状一致。"""
        monkeypatch.setattr(settings.wiki_compile, "WIKI_ROUTE_PRIOR_WEIGHT", 0.3)
        recommender = KBRecommender(FakeRagChain([kb_doc("kb-a", 0.5)]))

        async def empty_priors(question, kb_ids):
            return {}

        monkeypatch.setattr(recommender, "_get_route_priors", empty_priors)

        result = await recommender.recommend_knowledge_bases("问题", top_k=3)
        assert result[0]["relevance_score"] == pytest.approx(0.5)
        assert result[0]["matched_chunks"] == 1

    async def test_no_vector_store_returns_empty(self):
        class NoStore:
            def _has_vector_store(self):
                return False

        assert await KBRecommender(NoStore()).recommend_knowledge_bases("问题") == []

    async def test_get_route_priors_skips_on_zero_weight(self, monkeypatch):
        """权重为 0 时短路返回空 dict，不触发任何 DB/embedding 依赖。"""
        monkeypatch.setattr(settings.wiki_compile, "WIKI_ROUTE_PRIOR_WEIGHT", 0.0)
        recommender = KBRecommender(FakeRagChain([]))
        assert await recommender._get_route_priors("问题", ["kb-a"]) == {}
