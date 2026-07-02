"""检索与生成评估器单元测试。"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from src.services.evaluation import RetrievalEvaluator, GenerationEvaluator


class MockDoc:
    """模拟 LangChain Document 对象。"""

    def __init__(self, page_content, metadata=None):
        self.page_content = page_content
        self.metadata = metadata or {}


class TestRetrievalEvaluator:
    """检索评估器测试。"""

    @pytest.mark.asyncio
    async def test_empty_retrieved_docs(self):
        """空检索结果应返回零分。"""
        evaluator = RetrievalEvaluator()
        result = await evaluator.evaluate("question", [])
        assert result.hit_rate == 0.0
        assert result.mrr == 0.0
        assert result.context_precision == 0.0
        assert result.details.get("reason") == "检索结果为空"

    @pytest.mark.asyncio
    async def test_hit_rate_and_mrr_by_id(self):
        """基于期望 ID 计算命中率与 MRR。"""
        evaluator = RetrievalEvaluator()
        docs = [
            MockDoc("doc a", {"document_id": "d1"}),
            MockDoc("doc b", {"document_id": "d2"}),
            MockDoc("doc c", {"document_id": "d3"}),
        ]
        result = await evaluator.evaluate(
            "question", docs, expected_doc_ids=["d2"]
        )
        assert result.hit_rate == 1.0
        assert result.mrr == 0.5
        assert result.context_precision == pytest.approx(1 / 3, 0.01)
        assert result.context_recall == 1.0

    @pytest.mark.asyncio
    async def test_hit_rate_by_content(self):
        """基于期望内容子串判定相关性。"""
        evaluator = RetrievalEvaluator()
        docs = [
            MockDoc("Python is a programming language."),
            MockDoc("Java is also a language."),
        ]
        result = await evaluator.evaluate(
            "question", docs, expected_contents=["Python"]
        )
        assert result.hit_rate == 1.0
        assert result.mrr == 1.0
        assert result.context_precision == 0.5

    @pytest.mark.asyncio
    async def test_miss(self):
        """未命中时应返回零命中。"""
        evaluator = RetrievalEvaluator()
        docs = [MockDoc("doc a", {"document_id": "d1"})]
        result = await evaluator.evaluate(
            "question", docs, expected_doc_ids=["d2"]
        )
        assert result.hit_rate == 0.0
        assert result.mrr == 0.0
        assert result.context_precision == 0.0
        assert result.context_recall == 0.0

    @pytest.mark.asyncio
    async def test_batch_evaluation(self):
        """批量评估应返回聚合指标。"""
        evaluator = RetrievalEvaluator()
        samples = [
            {
                "question": "q1",
                "retrieved_docs": [MockDoc("a", {"document_id": "d1"})],
                "expected_doc_ids": ["d1"],
            },
            {
                "question": "q2",
                "retrieved_docs": [MockDoc("b", {"document_id": "d2"})],
                "expected_doc_ids": ["d3"],
            },
        ]
        report = await evaluator.evaluate_batch(samples)
        assert report["aggregated"]["hit_rate"] == 0.5
        assert report["aggregated"]["mrr"] == 0.5
        assert len(report["samples"]) == 2


class TestGenerationEvaluator:
    """生成评估器测试。"""

    @pytest.fixture
    def mock_llm(self):
        llm = MagicMock()
        llm.ainvoke = AsyncMock(
            return_value=MagicMock(content='{"score": 0.85, "reason": "good"}')
        )
        return llm

    @pytest.mark.asyncio
    async def test_parse_score_from_json(self, mock_llm):
        """应从 JSON 输出中解析分数。"""
        evaluator = GenerationEvaluator(llm=mock_llm)
        result = await evaluator.evaluate(
            answer="answer",
            question="question",
            contexts=["context"],
        )
        assert result.faithfulness == 0.85
        assert result.relevance == 0.85
        assert "good" in result.faithfulness_reason

    @pytest.mark.asyncio
    async def test_parse_score_from_text(self, mock_llm):
        """应从文本输出中解析分数。"""
        mock_llm.ainvoke = AsyncMock(return_value=MagicMock(content="Score: 0.7"))
        evaluator = GenerationEvaluator(llm=mock_llm)
        result = await evaluator.evaluate(
            answer="answer",
            question="question",
            contexts=["context"],
        )
        assert result.faithfulness == 0.7
        assert result.relevance == 0.7

    @pytest.mark.asyncio
    async def test_empty_answer(self, mock_llm):
        """空答案应返回零分。"""
        evaluator = GenerationEvaluator(llm=mock_llm)
        result = await evaluator.evaluate(
            answer="",
            question="question",
            contexts=["context"],
        )
        assert result.faithfulness == 0.0
        assert "为空" in result.faithfulness_reason

    @pytest.mark.asyncio
    async def test_llm_unavailable(self, monkeypatch):
        """LLM 不可用时评估应优雅降级。"""
        evaluator = GenerationEvaluator(llm=None)
        monkeypatch.setattr(evaluator, "_get_llm", AsyncMock(return_value=None))
        result = await evaluator.evaluate(
            answer="answer",
            question="question",
            contexts=["context"],
        )
        assert result.faithfulness == 0.0
        assert result.relevance == 0.0

    @pytest.mark.asyncio
    async def test_batch_evaluation(self, mock_llm):
        """批量生成评估应返回聚合指标。"""
        evaluator = GenerationEvaluator(llm=mock_llm)
        samples = [
            {"answer": "a1", "question": "q1", "contexts": ["c1"]},
            {"answer": "a2", "question": "q2", "contexts": ["c2"]},
        ]
        report = await evaluator.evaluate_batch(samples)
        assert report["aggregated"]["faithfulness"] == 0.85
        assert report["aggregated"]["relevance"] == 0.85
        assert len(report["samples"]) == 2
