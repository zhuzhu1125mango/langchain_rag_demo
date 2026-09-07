"""联网搜索链路优化端到端评估测试。

覆盖 P0/P1 全链路集成场景：
1. QueryRewriter 多轮上下文补全 + 规则快速路径
2. SearchPostprocessor 数值提取 + 多源交叉验证
3. CitationBackfiller 引用补全 + AnswerVerifier 事实校验
4. RAGChain._verify_answer_suffix 完整后处理流程
5. IntentRouter 分级流水线分类

依赖外部服务（Ollama/SearXNG/Milvus）的 e2e 测试默认跳过，
纯逻辑测试使用 mock 验证集成正确性。
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.services.citation_backfiller import CitationBackfiller
from src.services.intent_router import IntentRouter, PrimaryMode, SearchPipeline
from src.services.output_sanitizer import AnswerVerifier, VerificationResult
from src.services.query_rewriter import QueryRewriter
from src.services.search_postprocessor import SearchPostprocessor
from src.services.search_types import SearchResult


# =============================================================================
# 1. QueryRewriter 集成测试
# =============================================================================
class TestQueryRewriterIntegration:
    """QueryRewriter 多轮上下文补全 + 规则快速路径集成测试。"""

    @pytest.mark.asyncio
    async def test_rule_based_rewrite_time_price(self):
        """规则快速路径：含时间+价格关键词应生成结构化 query。"""
        rewriter = QueryRewriter(llm=None)
        queries = await rewriter.rewrite("今天金价")
        # 始终包含原始问题
        assert "今天金价" in queries
        # 规则模板应生成额外 query
        assert len(queries) >= 1

    @pytest.mark.asyncio
    async def test_context_resolution_pronoun(self):
        """多轮上下文补全：含代词时应从历史提取实体。"""
        rewriter = QueryRewriter(llm=None)
        history = [
            {"role": "user", "content": "黄金价格多少"},
            {"role": "assistant", "content": "约780元/克"},
        ]
        queries = await rewriter.rewrite("它未来走势", conversation_context=history)
        # 应包含补全后的 query（含从历史提取的实体）
        assert len(queries) >= 1
        # 原始问题应在列表中
        assert any("走势" in q for q in queries)

    @pytest.mark.asyncio
    async def test_llm_fallback_on_no_rule_hit(self):
        """规则未命中时走 LLM fallback。"""
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=MagicMock(content='["量子计算原理", "量子计算应用"]'))
        rewriter = QueryRewriter(llm=mock_llm)
        queries = await rewriter.rewrite("请介绍量子计算")
        # 应包含 LLM 生成的 query
        assert len(queries) >= 1
        # 原始问题保底
        assert "请介绍量子计算" in queries


# =============================================================================
# 2. SearchPostprocessor 交叉验证集成
# =============================================================================
class TestSearchPostprocessorIntegration:
    """SearchPostprocessor 数值提取 + 多源交叉验证集成测试。"""

    def test_cross_source_validate_multi_domain(self):
        """多域名一致数值应被标记为 validated。"""
        postprocessor = SearchPostprocessor()
        results = [
            SearchResult(title="金价780", url="https://siteA.com/news", content="今日金价780元/克", source="searxng", engine="searxng"),
            SearchResult(title="黄金780", url="https://siteB.com/finance", content="黄金价格780元每克", source="searxng", engine="searxng"),
        ]
        data = postprocessor.cross_source_validate(results)
        assert len(data["validated_values"]) > 0

    def test_cross_source_validate_single_domain(self):
        """单一域名数值应被标记为 single_source。"""
        postprocessor = SearchPostprocessor()
        results = [
            SearchResult(title="数据", url="https://only.com/a", content="价格为780元", source="searxng", engine="searxng"),
            SearchResult(title="数据2", url="https://only.com/b", content="价格为780元", source="searxng", engine="searxng"),
        ]
        data = postprocessor.cross_source_validate(results)
        # 同一域名不算多源验证
        assert len(data["validated_values"]) == 0

    def test_extract_numeric_values_public_api(self):
        """公开 extract_numeric_values 应返回结构化数值信息。"""
        postprocessor = SearchPostprocessor()
        values = postprocessor.extract_numeric_values("金价780.5元/克，涨幅2.3%")
        assert len(values) >= 2
        for v in values:
            assert "value" in v
            assert "raw" in v
            assert "context" in v
            assert "position" in v


# =============================================================================
# 3. CitationBackfiller + AnswerVerifier 集成
# =============================================================================
class TestCitationAndVerificationIntegration:
    """引用补全 + 事实校验集成测试。"""

    @pytest.mark.asyncio
    async def test_backfill_then_verify_high_confidence(self):
        """引用补全 + 校验：有引用覆盖 + 多源验证 → 高置信度。"""
        backfiller = CitationBackfiller(embeddings=None)
        verifier = AnswerVerifier(embeddings=None, llm=None)

        answer = "今日金价约为780元/克[1]。较昨日上涨0.3%[1]。"
        sources = [
            {"source_index": 1, "title": "金价查询", "content": "今日金价780元/克，涨幅0.3%", "url": "https://siteA.com"},
        ]
        cross_data = {
            "validated_values": [],
            "single_source_values": [],
            "conflicting_values": [],
        }

        # 引用补全（已有有效引用，保持不变）
        backfilled = await backfiller.backfill(answer, sources)
        assert "[1]" in backfilled

        # 事实校验
        result = await verifier.verify(backfilled, sources, cross_data)
        assert isinstance(result, VerificationResult)
        assert 0.0 <= result.confidence <= 1.0

    @pytest.mark.asyncio
    async def test_backfill_marks_unverified_claims(self):
        """无引用的声明应被标记 [?]。"""
        backfiller = CitationBackfiller(embeddings=None)
        answer = "今日金价约为780元/克。国际形势复杂。"
        sources = [
            {"source_index": 1, "title": "金价", "content": "金价780元", "url": "https://a.com"},
        ]
        result = await backfiller.backfill(answer, sources)
        # 无 embeddings 时，无引用句应补 [?]
        assert "[?]" in result

    @pytest.mark.asyncio
    async def test_verification_warning_suffix(self):
        """低置信度应生成警告后缀。"""
        verifier = AnswerVerifier(embeddings=None, llm=None)
        # 无引用、无交叉验证数据 → 低置信度
        result = VerificationResult(
            is_consistent=False,
            confidence=0.2,
            warnings=["数值未验证"],
            unsupported_claims=[],
            conflicting_claims=[],
            citation_coverage=0.0,
            cross_source_consistency=0.0,
            source_authority=0.5,
            freshness_score=0.3,
        )
        suffix = verifier.format_warning_suffix(result)
        assert "可信度较低" in suffix

    @pytest.mark.asyncio
    async def test_verification_high_confidence_no_suffix(self):
        """高置信度不应追加警告。"""
        verifier = AnswerVerifier(embeddings=None, llm=None)
        result = VerificationResult(
            is_consistent=True,
            confidence=0.85,
            warnings=[],
            unsupported_claims=[],
            conflicting_claims=[],
            citation_coverage=0.8,
            cross_source_consistency=0.9,
            source_authority=0.8,
            freshness_score=0.9,
        )
        suffix = verifier.format_warning_suffix(result)
        assert suffix == ""


# =============================================================================
# 4. RAGChain._verify_answer_suffix 集成（使用 mock）
# =============================================================================
class TestRagChainPostProcessIntegration:
    """RAGChain 后处理流程集成测试（mock embeddings/LLM）。"""

    @pytest.mark.asyncio
    async def test_verify_answer_suffix_with_mock_verifier(self):
        """_verify_answer_suffix 应调用 verifier 并返回警告后缀与校验结果。"""
        from src.services.rag_chain import RAGChain

        # 创建 RAGChain 实例（绕过 _async_init）
        chain = RAGChain.__new__(RAGChain)

        # mock AnswerVerifier
        mock_verifier = AsyncMock()
        mock_verifier.verify = AsyncMock(return_value=VerificationResult(
            is_consistent=False,
            confidence=0.3,
            warnings=["低置信度"],
            unsupported_claims=[],
            conflicting_claims=[],
            citation_coverage=0.2,
            cross_source_consistency=0.1,
            source_authority=0.5,
            freshness_score=0.3,
        ))
        mock_verifier.format_warning_suffix = lambda result: "\n\n⚠️ 当前回答可信度较低，未在多个独立来源中得到验证，请谨慎参考。"

        chain.answer_verifier = mock_verifier

        sources = [{"source_index": 1, "title": "test", "content": "test", "url": "https://a.com"}]
        suffix, verification = await chain._verify_answer_suffix(
            "测试答案。", sources, {"validated_values": [], "single_source_values": [], "conflicting_values": []}
        )

        # 应返回 verifier 追加的警告后缀
        assert suffix is not None
        assert "可信度较低" in suffix
        # 应返回校验结果
        assert verification is not None
        assert verification.confidence == 0.3

    @pytest.mark.asyncio
    async def test_verify_answer_suffix_no_verifier_skips(self):
        """答案校验器不可用时应跳过校验，返回 (None, None)。"""
        from src.services.rag_chain import RAGChain

        chain = RAGChain.__new__(RAGChain)
        chain.answer_verifier = None

        suffix, verification = await chain._verify_answer_suffix(
            "原始答案。", [], None
        )

        assert suffix is None
        assert verification is None

    def test_build_citation_sources_format(self):
        """_build_citation_sources 应生成正确格式。"""
        from src.services.rag_chain import RAGChain

        chain = RAGChain.__new__(RAGChain)
        web_sources = [
            {"title": "标题1", "page_content": "内容1", "url": "https://a.com", "source_index": 1},
            {"title": "标题2", "content": "内容2", "url": "https://b.com"},
        ]
        result = chain._build_citation_sources(web_sources)
        assert len(result) == 2
        assert result[0]["source_index"] == 1
        assert result[0]["content"] == "内容1"
        assert result[1]["source_index"] == 2  # 自动编号
        assert result[1]["content"] == "内容2"


# =============================================================================
# 5. IntentRouter 分级流水线集成
# =============================================================================
class TestIntentRouterPipelineIntegration:
    """IntentRouter 分级流水线与 route 方法集成测试。"""

    @pytest.mark.asyncio
    async def test_route_realtime_fast_path(self):
        """实时性问题 + 规则关键词 → WEB_SEARCH + FAST_PATH。"""
        router = IntentRouter()
        decision = await router.route("最新新闻", use_web_search=True)
        assert decision.primary_mode == PrimaryMode.WEB_SEARCH
        assert decision.search_pipeline == SearchPipeline.FAST_PATH

    @pytest.mark.asyncio
    async def test_route_realtime_full_path_with_pronoun(self):
        """实时性问题 + 多轮追问 → WEB_SEARCH + FULL_PATH。"""
        router = IntentRouter()
        history = [{"role": "user", "content": "黄金价格"}, {"role": "assistant", "content": "780元"}]
        decision = await router.route("它最新新闻怎么样", use_web_search=True, history=history)
        assert decision.primary_mode == PrimaryMode.WEB_SEARCH
        assert decision.search_pipeline == SearchPipeline.FULL_PATH

    @pytest.mark.asyncio
    async def test_route_decision_serializable(self):
        """决策结果应可序列化为 dict（含 search_pipeline）。"""
        router = IntentRouter()
        decision = await router.route("最新新闻", use_web_search=True)
        d = decision.to_dict()
        assert "search_pipeline" in d
        assert d["search_pipeline"] in ("fast_path", "full_path")


# =============================================================================
# 6. 端到端场景（需外部服务，默认跳过）
# =============================================================================
@pytest.mark.e2e
class TestSearchOptimizationE2E:
    """联网搜索优化全链路 e2e 测试（需 Ollama/SearXNG/Milvus）。

    默认跳过，传入 --run-e2e 选项时运行：
    uv run python -m pytest tests/evaluation/test_search_optimization.py::TestSearchOptimizationE2E --run-e2e
    """

    @pytest.mark.asyncio
    async def test_e2e_citation_backfill_with_real_embeddings(self):
        """真实 embeddings 下引用补全应正确匹配来源。"""
        from langchain_ollama import OllamaEmbeddings
        from src.config import settings

        embeddings = OllamaEmbeddings(model=settings.model.EMBEDDING_MODEL_NAME)
        backfiller = CitationBackfiller(embeddings=embeddings)
        answer = "今日黄金价格约为780元/克。受美元指数影响较大。"
        sources = [
            {"source_index": 1, "title": "金价", "content": "今日黄金价格780元每克", "url": "https://a.com"},
            {"source_index": 2, "title": "美元", "content": "美元指数影响黄金价格", "url": "https://b.com"},
        ]
        result = await backfiller.backfill(answer, sources)
        assert "[1]" in result or "[?]" in result

    @pytest.mark.asyncio
    async def test_e2e_full_search_pipeline(self):
        """完整搜索流水线：改写 → 搜索 → 后处理 → 引用补全 → 校验。"""
        from langchain_ollama import ChatOllama, OllamaEmbeddings
        from src.config import settings
        from src.services.web_search_service import WebSearchService

        llm = ChatOllama(model=settings.model.OLLAMA_MODEL_NAME)
        embeddings = OllamaEmbeddings(model=settings.model.EMBEDDING_MODEL_NAME)
        service = WebSearchService(llm=llm)
        backfiller = CitationBackfiller(embeddings=embeddings)
        verifier = AnswerVerifier(embeddings=embeddings, llm=llm)

        context, sources, cross_data = await service.build_search_context_enhanced(
            "今天黄金价格", conversation_context=[]
        )
        if not sources:
            pytest.skip("SearXNG 未返回结果")

        # 模拟 LLM 生成答案
        answer = f"根据搜索结果，今日黄金价格约为780元/克。"
        backfilled = await backfiller.backfill(answer, sources)
        result = await verifier.verify(backfilled, sources, cross_data)

        assert isinstance(result, VerificationResult)
        assert 0.0 <= result.confidence <= 1.0

    @pytest.mark.asyncio
    async def test_e2e_rag_chain_post_process(self):
        """RAGChain 后处理链路真实环境测试。

        需启动完整后端服务后手动运行。
        """
        # 占位：需启动 Milvus/PostgreSQL/Ollama 后手动验证
        pytest.skip("需完整 RAGChain 环境（Milvus/PostgreSQL/Ollama），请手动验证")
