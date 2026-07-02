"""Embedding 意图分类器单元测试。

使用 mock embedding 避免依赖外部 Ollama 服务。
"""

import pytest

from src.services.intent_router.embedding_classifier import (
    EmbeddingIntentClassifier,
    EmbeddingClassifierResult,
)
from src.services.intent_router.intent_examples import IntentExampleStore
from src.services.intent_router.models import PrimaryMode


class MockEmbeddings:
    """模拟 Embedding 模型：将文本映射为确定性低维向量。"""

    def __init__(self, dim: int = 8):
        self.dim = dim

    def _vec(self, text: str) -> list:
        # 为相同文本生成确定性向量，并保留部分相似结构
        seed = hash(text.strip()) % 10000
        import random
        rng = random.Random(seed)
        return [rng.random() for _ in range(self.dim)]

    async def aembed_query(self, text: str) -> list:
        return self._vec(text)

    async def aembed_documents(self, texts: list) -> list:
        return [self._vec(t) for t in texts]


@pytest.fixture
def mock_embeddings():
    return MockEmbeddings(dim=8)


@pytest.fixture
def classifier(mock_embeddings):
    store = IntentExampleStore({
        PrimaryMode.DIRECT_LLM.value: ["你好", "hello"],
        PrimaryMode.KB_ONLY.value: ["文档里怎么说的", "请查阅制度"],
        PrimaryMode.TOOL_FIRST.value: ["北京天气", "现在几点"],
        PrimaryMode.WEB_SEARCH.value: ["今天新闻", "最新金价"],
    })
    return EmbeddingIntentClassifier(
        embeddings=mock_embeddings,
        example_store=store,
        similarity_threshold=0.5,
        ambiguity_gap=0.1,
    )


class TestIntentExampleStore:
    """意图示例库测试。"""

    def test_default_examples_loaded(self):
        store = IntentExampleStore()
        assert len(store.get_examples(PrimaryMode.DIRECT_LLM.value)) > 0
        assert len(store.get_examples(PrimaryMode.KB_ONLY.value)) > 0

    def test_add_and_remove_examples(self):
        store = IntentExampleStore()
        store.add_examples(PrimaryMode.DIRECT_LLM.value, ["测试问候"])
        assert "测试问候" in store.get_examples(PrimaryMode.DIRECT_LLM.value)
        store.remove_examples(PrimaryMode.DIRECT_LLM.value, ["测试问候"])
        assert "测试问候" not in store.get_examples(PrimaryMode.DIRECT_LLM.value)

    def test_duplicate_examples_ignored(self):
        store = IntentExampleStore()
        count_before = len(store.get_examples(PrimaryMode.DIRECT_LLM.value))
        store.add_examples(PrimaryMode.DIRECT_LLM.value, ["你好"])
        count_after = len(store.get_examples(PrimaryMode.DIRECT_LLM.value))
        assert count_after == count_before


class TestEmbeddingClassifier:
    """Embedding 分类器核心逻辑测试。"""

    @pytest.mark.asyncio
    async def test_classify_empty_question(self, classifier):
        result = await classifier.classify("")
        assert result.primary_mode == PrimaryMode.DIRECT_LLM
        assert result.confidence == 0.0

    @pytest.mark.asyncio
    async def test_classify_returns_scores(self, classifier):
        result = await classifier.classify("你好")
        assert isinstance(result.confidence_scores, dict)
        assert PrimaryMode.DIRECT_LLM.value in result.confidence_scores
        assert result.confidence > 0

    @pytest.mark.asyncio
    async def test_classify_knowledge_base_question(self, classifier):
        result = await classifier.classify("文档里怎么说的")
        assert result.primary_mode == PrimaryMode.KB_ONLY
        assert result.confidence >= classifier.similarity_threshold

    @pytest.mark.asyncio
    async def test_classify_tool_question(self, classifier):
        result = await classifier.classify("北京今天天气")
        assert result.primary_mode == PrimaryMode.TOOL_FIRST

    @pytest.mark.asyncio
    async def test_low_confidence_degrades(self, classifier):
        # 使用与示例都不相关的问题，应低于阈值
        result = await classifier.classify("xyz abc 12345")
        assert result.confidence < classifier.similarity_threshold
        assert result.primary_mode == PrimaryMode.DIRECT_LLM

    def test_to_intent_decision_knowledge_base_without_kb(self, classifier):
        result = EmbeddingClassifierResult(
            primary_mode=PrimaryMode.KB_ONLY,
            confidence=0.9,
            confidence_scores={"kb_only": 0.9},
            reasoning="test",
        )
        decision = classifier.to_intent_decision(result, "文档里怎么说的", has_kb=False)
        assert decision.primary_mode == PrimaryMode.DIRECT_LLM

    def test_to_intent_decision_web_search(self, classifier):
        result = EmbeddingClassifierResult(
            primary_mode=PrimaryMode.WEB_SEARCH,
            confidence=0.9,
            confidence_scores={"web_search": 0.9},
            reasoning="test",
        )
        decision = classifier.to_intent_decision(result, "今天新闻", use_web_search=True)
        assert decision.needs_web is True
        assert "web_search" in decision.suggested_tools

    def test_to_intent_decision_ambiguity_triggers_clarify(self, classifier):
        scores = {
            PrimaryMode.KB_ONLY.value: 0.55,
            PrimaryMode.WEB_SEARCH.value: 0.50,
        }
        result = EmbeddingClassifierResult(
            primary_mode=PrimaryMode.KB_ONLY,
            confidence=0.55,
            confidence_scores=scores,
            reasoning="test",
        )
        decision = classifier.to_intent_decision(result, "查一下这个", has_kb=True)
        assert decision.needs_clarify is True

    @pytest.mark.asyncio
    async def test_learn_from_feedback_adds_example(self, classifier):
        await classifier.learn_from_feedback(
            question="新示例问题",
            predicted_mode=PrimaryMode.DIRECT_LLM.value,
            actual_mode=PrimaryMode.KB_ONLY.value,
            confidence=0.7,
        )
        assert "新示例问题" in classifier.example_store.get_examples(PrimaryMode.KB_ONLY.value)

    @pytest.mark.asyncio
    async def learn_from_feedback_correct_no_change(self, classifier):
        result = await classifier.learn_from_feedback(
            question="你好",
            predicted_mode=PrimaryMode.DIRECT_LLM.value,
            actual_mode=PrimaryMode.DIRECT_LLM.value,
            confidence=0.9,
        )
        assert result["status"] == "correct"

    @pytest.mark.asyncio
    async def test_batch_learn(self, classifier):
        classifier.record_feedback("问题A", "direct_llm", "kb_only", 0.7)
        classifier.record_feedback("问题B", "direct_llm", "direct_llm", 0.9)
        result = await classifier.batch_learn()
        assert result["status"] == "success"
        assert result["processed"] == 2
        assert result["learned"] == 1
        assert result["correct"] == 1


class TestEmbeddingClassifierTools:
    """工具推断测试。"""

    def test_infer_time_tool(self, classifier):
        tools = classifier._infer_tools_from_question("现在几点")
        assert "get_current_time" in tools

    def test_infer_weather_tool(self, classifier):
        tools = classifier._infer_tools_from_question("北京天气")
        assert "weather_query" in tools

    def test_infer_calculator_default(self, classifier):
        tools = classifier._infer_tools_from_question("随便问问")
        assert "calculator" in tools
