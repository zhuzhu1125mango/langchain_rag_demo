"""citation_backfiller 单元测试。

覆盖切句、已有引用校验、embedding 匹配补全、降级策略等场景。
使用 Mock embeddings 避免依赖 Ollama 服务。
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.citation_backfiller import CitationBackfiller


def _make_embeddings(query_emb=None, doc_embs=None, raise_on_query=False, raise_on_doc=False):
    """构造 Mock embeddings 实例。

    Args:
        query_emb: aembed_query 返回的向量。
        doc_embs: aembed_documents 返回的向量列表。
        raise_on_query: aembed_query 是否抛异常。
        raise_on_doc: aembed_documents 是否抛异常。
    """
    emb = MagicMock()
    if raise_on_query:
        emb.aembed_query = AsyncMock(side_effect=RuntimeError("ollama down"))
    else:
        emb.aembed_query = AsyncMock(return_value=query_emb or [1.0, 0.0, 0.0])
    if raise_on_doc:
        emb.aembed_documents = AsyncMock(side_effect=RuntimeError("ollama down"))
    else:
        emb.aembed_documents = AsyncMock(return_value=doc_embs or [[1.0, 0.0, 0.0]])
    return emb


class TestSplitSentences:
    """切句逻辑测试。"""

    @pytest.mark.asyncio
    async def test_chinese_sentence_split(self):
        """中文句号应正确切句。"""
        backfiller = CitationBackfiller(embeddings=None)
        sentences = backfiller._split_sentences("今天天气很好。明天会下雨。")
        assert len(sentences) == 2

    @pytest.mark.asyncio
    async def test_english_sentence_split(self):
        """英文句末标点应正确切句。"""
        backfiller = CitationBackfiller(embeddings=None)
        sentences = backfiller._split_sentences("Hello world! How are you? Fine.")
        assert len(sentences) == 3

    @pytest.mark.asyncio
    async def test_format_lines_preserved(self):
        """格式行（分隔线、标题）应保留但不做引用补全。"""
        backfiller = CitationBackfiller(embeddings=None)
        text = "### 标题\n---\n这是正文的句子内容。\n"
        sentences = backfiller._split_sentences(text)
        # 应保留所有部分
        assert len(sentences) >= 2

    @pytest.mark.asyncio
    async def test_empty_text(self):
        """空文本应返回空列表。"""
        backfiller = CitationBackfiller(embeddings=None)
        assert backfiller._split_sentences("") == []
        assert backfiller._split_sentences(None) == []  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_short_sentence_kept(self):
        """短句（< 5 字）应保留在输出中但不做补全。"""
        backfiller = CitationBackfiller(embeddings=None)
        sentences = backfiller._split_sentences("好的。这是较长的正文句子。")
        assert len(sentences) == 2


class TestValidateExistingCitations:
    """已有引用校验测试。"""

    @pytest.mark.asyncio
    async def test_valid_citation_kept(self):
        """有效引用 [n] 应保留。"""
        backfiller = CitationBackfiller(embeddings=None)
        result = backfiller._validate_existing_citations(
            "金价 850 元[1]。", ["1"], {1, 2}
        )
        assert "[1]" in result

    @pytest.mark.asyncio
    async def test_invalid_citation_marked_unknown(self):
        """超出范围的引用 [n] 应改为 [?]。"""
        backfiller = CitationBackfiller(embeddings=None)
        result = backfiller._validate_existing_citations(
            "金价 850 元[5]。", ["5"], {1, 2}
        )
        assert "[?]" in result
        assert "[5]" not in result

    @pytest.mark.asyncio
    async def test_no_sources_all_marked_unknown(self):
        """无有效来源时所有 [n] 应改为 [?]。"""
        backfiller = CitationBackfiller(embeddings=None)
        result = backfiller._validate_existing_citations(
            "金价 850 元[1]。银价 30 元[2]。", ["1", "2"], set()
        )
        assert result.count("[?]") == 2

    @pytest.mark.asyncio
    async def test_existing_question_mark_kept(self):
        """已存在的 [?] 应保留不变。"""
        backfiller = CitationBackfiller(embeddings=None)
        result = backfiller._validate_existing_citations(
            "某声明[?]。", ["?"], {1, 2}
        )
        assert "[?]" in result


class TestBackfillCitation:
    """embedding 匹配补全测试。"""

    @pytest.mark.asyncio
    async def test_high_score_backfilled_with_index(self):
        """相似度 > 阈值应补 [n]。"""
        # query 和 source 1 完全一致，相似度 = 1.0
        embeddings = _make_embeddings(
            query_emb=[1.0, 0.0, 0.0],
            doc_embs=[[1.0, 0.0, 0.0]],
        )
        backfiller = CitationBackfiller(embeddings=embeddings)
        source_embs = [(1, [1.0, 0.0, 0.0])]
        result = await backfiller._backfill_citation("今日金价上涨明显。", source_embs)
        assert "[1]" in result

    @pytest.mark.asyncio
    async def test_low_score_marked_unknown(self):
        """相似度 ≤ 阈值应补 [?]。"""
        # query 和 source 正交，相似度 = 0.0
        embeddings = _make_embeddings(
            query_emb=[0.0, 1.0, 0.0],
            doc_embs=[[1.0, 0.0, 0.0]],
        )
        backfiller = CitationBackfiller(embeddings=embeddings)
        source_embs = [(1, [1.0, 0.0, 0.0])]
        result = await backfiller._backfill_citation("今日金价上涨明显。", source_embs)
        assert "[?]" in result

    @pytest.mark.asyncio
    async def test_no_embeddings_marked_unknown(self):
        """embeddings 不可用时应补 [?]。"""
        backfiller = CitationBackfiller(embeddings=None)
        result = await backfiller._backfill_citation("今日金价上涨明显。", [])
        assert "[?]" in result

    @pytest.mark.asyncio
    async def test_no_sources_marked_unknown(self):
        """无来源时应补 [?]。"""
        embeddings = _make_embeddings()
        backfiller = CitationBackfiller(embeddings=embeddings)
        result = await backfiller._backfill_citation("今日金价上涨明显。", [])
        assert "[?]" in result

    @pytest.mark.asyncio
    async def test_query_failure_degrades_to_unknown(self):
        """embedding 查询失败时应降级为 [?]。"""
        embeddings = _make_embeddings(raise_on_query=True)
        backfiller = CitationBackfiller(embeddings=embeddings)
        source_embs = [(1, [1.0, 0.0, 0.0])]
        result = await backfiller._backfill_citation("今日金价上涨明显。", source_embs)
        assert "[?]" in result


class TestAppendCitation:
    """引用追加位置测试。"""

    def test_append_before_chinese_period(self):
        """中文句号前应插入引用。"""
        result = CitationBackfiller._append_citation("金价 850 元。", "1")
        assert result == "金价 850 元[1]。"

    def test_append_before_exclamation(self):
        """感叹号前应插入引用。"""
        result = CitationBackfiller._append_citation("大涨了！", "2")
        assert result == "大涨了[2]！"

    def test_append_at_end_no_punctuation(self):
        """无句末标点时引用追加在末尾。"""
        result = CitationBackfiller._append_citation("金价 850 元", "1")
        assert result == "金价 850 元[1]"

    def test_append_preserves_trailing_whitespace(self):
        """应保留句尾空白。"""
        result = CitationBackfiller._append_citation("金价 850 元。  \n", "1")
        assert result == "金价 850 元[1]。  \n"


class TestCosineSimilarity:
    """余弦相似度计算测试。"""

    def test_identical_vectors(self):
        """相同向量相似度应为 1.0。"""
        score = CitationBackfiller._cosine_similarity([1.0, 0.0], [1.0, 0.0])
        assert abs(score - 1.0) < 1e-6

    def test_orthogonal_vectors(self):
        """正交向量相似度应为 0.0。"""
        score = CitationBackfiller._cosine_similarity([1.0, 0.0], [0.0, 1.0])
        assert abs(score) < 1e-6

    def test_zero_vector(self):
        """零向量相似度应为 0.0。"""
        score = CitationBackfiller._cosine_similarity([0.0, 0.0], [1.0, 1.0])
        assert score == 0.0

    def test_different_length(self):
        """不同长度向量相似度应为 0.0。"""
        score = CitationBackfiller._cosine_similarity([1.0], [1.0, 2.0])
        assert score == 0.0

    def test_empty_vectors(self):
        """空向量相似度应为 0.0。"""
        assert CitationBackfiller._cosine_similarity([], []) == 0.0


class TestFindBestMatch:
    """最佳匹配测试。"""

    def test_best_match_found(self):
        """应返回相似度最高的 source。"""
        backfiller = CitationBackfiller(embeddings=None)
        query = [1.0, 0.0, 0.0]
        sources = [
            (1, [0.0, 1.0, 0.0]),  # 0.0
            (2, [1.0, 0.0, 0.0]),  # 1.0 ← 最佳
            (3, [0.5, 0.5, 0.0]),  # 0.707
        ]
        idx, score = backfiller._find_best_match(query, sources)
        assert idx == 2
        assert score > 0.99

    def test_empty_sources(self):
        """无来源应返回 (None, 0.0)。"""
        backfiller = CitationBackfiller(embeddings=None)
        idx, score = backfiller._find_best_match([1.0, 0.0], [])
        assert idx is None
        assert score == 0.0


class TestBackfillIntegration:
    """backfill 集成测试。"""

    @pytest.mark.asyncio
    async def test_empty_answer(self):
        """空答案应原样返回。"""
        backfiller = CitationBackfiller(embeddings=None)
        assert await backfiller.backfill("", []) == ""
        assert await backfiller.backfill("   ", []) == "   "

    @pytest.mark.asyncio
    async def test_no_sources_validates_existing(self):
        """无来源时已有 [n] 全部改为 [?]。"""
        backfiller = CitationBackfiller(embeddings=None)
        answer = "金价 850 元[1]。银价 30 元[2]。"
        result = await backfiller.backfill(answer, [])
        assert result.count("[?]") == 2

    @pytest.mark.asyncio
    async def test_valid_citations_preserved(self):
        """有效引用应保留，无引用句补 [?]（无 embeddings 时）。"""
        backfiller = CitationBackfiller(embeddings=None)
        answer = "金价 850 元[1]。这是另一句事实声明。"
        sources = [{"source_index": 1, "title": "金价", "content": "850 元"}]
        result = await backfiller.backfill(answer, sources)
        # 第一句 [1] 保留
        assert "[1]" in result
        # 第二句无 embeddings，补 [?]
        assert "[?]" in result

    @pytest.mark.asyncio
    async def test_with_embeddings_backfill(self):
        """有 embeddings 时应基于相似度补全。"""
        embeddings = _make_embeddings(
            query_emb=[1.0, 0.0, 0.0],
            doc_embs=[[1.0, 0.0, 0.0]],
        )
        backfiller = CitationBackfiller(embeddings=embeddings)
        answer = "今日金价上涨明显。"
        sources = [{"source_index": 1, "title": "金价", "content": "上涨"}]
        result = await backfiller.backfill(answer, sources)
        assert "[1]" in result

    @pytest.mark.asyncio
    async def test_doc_embedding_failure_degrades(self):
        """source embedding 计算失败时应降级为 [?]。"""
        embeddings = _make_embeddings(raise_on_doc=True)
        backfiller = CitationBackfiller(embeddings=embeddings)
        answer = "今日金价上涨明显。"
        sources = [{"source_index": 1, "title": "金价", "content": "上涨"}]
        result = await backfiller.backfill(answer, sources)
        assert "[?]" in result

    @pytest.mark.asyncio
    async def test_format_lines_not_backfilled(self):
        """格式行不应被追加引用。"""
        backfiller = CitationBackfiller(embeddings=None)
        answer = "### 金价报告\n---\n今日金价 850 元。\n"
        sources = [{"source_index": 1, "title": "金价", "content": "850"}]
        result = await backfiller.backfill(answer, sources)
        # 标题和分隔线不应有引用
        assert "### 金价报告" in result
        assert "---" in result
        # 正文句应补引用（无 embeddings → [?]）
        assert "[?]" in result


class TestCollectValidIndices:
    """有效 source_index 收集测试。"""

    def test_normal_sources(self):
        backfiller = CitationBackfiller(embeddings=None)
        sources = [
            {"source_index": 1, "title": "a"},
            {"source_index": 2, "title": "b"},
        ]
        assert backfiller._collect_valid_indices(sources) == {1, 2}

    def test_invalid_indices_filtered(self):
        backfiller = CitationBackfiller(embeddings=None)
        sources = [
            {"source_index": 0, "title": "a"},  # 0 无效
            {"source_index": -1, "title": "b"},  # 负数无效
            {"source_index": "x", "title": "c"},  # 非整数无效
            {"title": "d"},  # 缺失
            {"source_index": 3, "title": "e"},  # 有效
        ]
        assert backfiller._collect_valid_indices(sources) == {3}

    def test_empty_sources(self):
        backfiller = CitationBackfiller(embeddings=None)
        assert backfiller._collect_valid_indices([]) == set()
        assert backfiller._collect_valid_indices(None) == set()  # type: ignore[arg-type]
