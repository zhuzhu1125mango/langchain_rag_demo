"""检索与生成评估器单元测试。"""

import pytest

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


class FakeEmbeddings:
    """按固定词表做 0/1 词袋的假 embedding（同词表命中→同向，无命中→零向量）。"""

    VOCAB = ("事假", "远程办公", "直属主管", "工作日", "OA", "天气", "散步")

    def __init__(self, fail: bool = False):
        self.fail = fail

    async def aembed_documents(self, texts):
        if self.fail:
            raise ConnectionError("ollama down")
        return [[1.0 if w in t else 0.0 for w in self.VOCAB] for t in texts]


class TestGenerationEvaluator:
    """生成评估器测试（确定性规则，无 LLM）。"""

    CONTEXTS = [
        "事假管理：员工请事假须提前3个工作日提交申请，经直属主管审批。",
        "远程办公：员工须提前2个工作日在OA系统提交远程办公申请，单次不超过5个工作日。",
    ]

    # ---------------- 忠实度 ----------------

    @pytest.mark.asyncio
    async def test_faithfulness_grounded_is_one(self, monkeypatch):
        """完全有依据的答案应得 1.0。"""
        evaluator = GenerationEvaluator()
        monkeypatch.setattr(evaluator, "_get_embeddings", lambda: FakeEmbeddings())
        result = await evaluator.evaluate_faithfulness(
            "事假需提前3个工作日申请，远程办公需提前2个工作日在OA提交。", self.CONTEXTS
        )
        assert result["score"] == 1.0
        assert "2/2" in result["reason"]

    @pytest.mark.asyncio
    async def test_faithfulness_numeric_contradiction_is_zero(self, monkeypatch):
        """数字与上下文矛盾（3→30）即使语义高度相似也判 0。"""
        evaluator = GenerationEvaluator()
        monkeypatch.setattr(evaluator, "_get_embeddings", lambda: FakeEmbeddings())
        result = await evaluator.evaluate_faithfulness(
            "事假需提前30个工作日申请。", self.CONTEXTS
        )
        assert result["score"] == 0.0

    @pytest.mark.asyncio
    async def test_faithfulness_fabricated_and_offtopic_are_zero(self, monkeypatch):
        evaluator = GenerationEvaluator()
        monkeypatch.setattr(evaluator, "_get_embeddings", lambda: FakeEmbeddings())
        assert (await evaluator.evaluate_faithfulness(
            "公司规定远程办公期间工资按80%发放。", self.CONTEXTS
        ))["score"] == 0.0
        assert (await evaluator.evaluate_faithfulness(
            "今天天气不错，适合出门散步。", self.CONTEXTS
        ))["score"] == 0.0

    @pytest.mark.asyncio
    async def test_faithfulness_empty_answer_or_context(self, monkeypatch):
        evaluator = GenerationEvaluator()
        monkeypatch.setattr(evaluator, "_get_embeddings", lambda: FakeEmbeddings())
        assert "为空" in (await evaluator.evaluate_faithfulness("", self.CONTEXTS))["reason"]
        assert (await evaluator.evaluate_faithfulness("事假需提前3个工作日申请。", []))["score"] == 0.0

    @pytest.mark.asyncio
    async def test_faithfulness_strips_confidence_disclaimer(self, monkeypatch):
        """置信度声明是展示层产物，不应影响判分。"""
        evaluator = GenerationEvaluator()
        monkeypatch.setattr(evaluator, "_get_embeddings", lambda: FakeEmbeddings())
        dirty = (
            "事假需提前3个工作日申请。[1]\n\n"
            "⚠️ 当前回答可信度较低，未在多个独立来源中得到验证，请谨慎参考。"
        )
        assert (await evaluator.evaluate_faithfulness(dirty, self.CONTEXTS))["score"] == 1.0

    @pytest.mark.asyncio
    async def test_faithfulness_falls_back_to_lexical_when_embedding_fails(self, monkeypatch):
        evaluator = GenerationEvaluator()
        monkeypatch.setattr(evaluator, "_get_embeddings", lambda: FakeEmbeddings(fail=True))
        result = await evaluator.evaluate_faithfulness(
            "事假需提前3个工作日申请，远程办公需提前2个工作日在OA提交。", self.CONTEXTS
        )
        assert result["score"] == 1.0
        assert "词面兜底" in result["reason"]

    # ---------------- 相关度 ----------------

    @pytest.mark.asyncio
    async def test_relevance_high_for_related_answer(self, monkeypatch):
        evaluator = GenerationEvaluator()
        monkeypatch.setattr(evaluator, "_get_embeddings", lambda: FakeEmbeddings())
        result = await evaluator.evaluate_relevance(
            "远程办公需提前2个工作日申请。", "远程办公需提前2个工作日申请？"
        )
        assert result["score"] == 1.0

    @pytest.mark.asyncio
    async def test_relevance_zero_for_offtopic_answer(self, monkeypatch):
        evaluator = GenerationEvaluator()
        monkeypatch.setattr(evaluator, "_get_embeddings", lambda: FakeEmbeddings())
        result = await evaluator.evaluate_relevance(
            "今天天气不错，适合出门散步。", "远程办公要提前多久申请？"
        )
        assert result["score"] == 0.0

    @pytest.mark.asyncio
    async def test_relevance_falls_back_to_lexical_when_embedding_fails(self, monkeypatch):
        evaluator = GenerationEvaluator()
        monkeypatch.setattr(evaluator, "_get_embeddings", lambda: FakeEmbeddings(fail=True))
        result = await evaluator.evaluate_relevance(
            "远程办公需提前2个工作日申请。", "远程办公要提前多久申请？"
        )
        assert 0.0 < result["score"] <= 1.0
        assert "词面覆盖" in result["reason"]

    # ---------------- 聚合 ----------------

    @pytest.mark.asyncio
    async def test_evaluate_and_batch(self, monkeypatch):
        evaluator = GenerationEvaluator()
        monkeypatch.setattr(evaluator, "_get_embeddings", lambda: FakeEmbeddings())
        result = await evaluator.evaluate(
            answer="事假需提前3个工作日申请，远程办公需提前2个工作日在OA提交。",
            question="远程办公需提前2个工作日申请？",
            contexts=self.CONTEXTS,
        )
        assert result.faithfulness == 1.0
        assert result.relevance > 0.0

        report = await evaluator.evaluate_batch([
            {"answer": "事假需提前3个工作日申请。", "question": "远程办公要提前多久？", "contexts": self.CONTEXTS},
            {"answer": "", "question": "q", "contexts": self.CONTEXTS},
        ])
        assert report["aggregated"]["faithfulness"] == 0.5
        assert len(report["samples"]) == 2

    @pytest.mark.asyncio
    async def test_empty_answer_scores_zero(self, monkeypatch):
        evaluator = GenerationEvaluator()
        monkeypatch.setattr(evaluator, "_get_embeddings", lambda: FakeEmbeddings())
        result = await evaluator.evaluate(answer="", question="q", contexts=self.CONTEXTS)
        assert result.faithfulness == 0.0
        assert "为空" in result.faithfulness_reason
