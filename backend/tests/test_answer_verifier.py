"""answer_verifier 单元测试。

覆盖 AnswerVerifier 的数字一致性、日期一致性、引用真实性、
多源交叉一致性、引用覆盖率、置信度计算与警告输出等场景。
"""

from decimal import Decimal

import pytest

from src.services.output_sanitizer import AnswerVerifier, VerificationResult


@pytest.fixture
def verifier() -> AnswerVerifier:
    """答案校验器实例（不依赖 LLM/embeddings）。"""
    return AnswerVerifier(embeddings=None, llm=None)


def _make_sources(*items) -> list:
    """构造来源列表。

    Args:
        items: 每项为 (source_index, title, content, url) 元组。
    """
    sources = []
    for item in items:
        idx, title, content, url = item
        sources.append({
            "source_index": idx,
            "title": title,
            "content": content,
            "url": url,
        })
    return sources


class TestEmptyAnswer:
    """空答案边界测试。"""

    @pytest.mark.asyncio
    async def test_empty_answer(self, verifier):
        """空答案应返回高置信。"""
        result = await verifier.verify("", [])
        assert result.is_consistent is True
        assert result.confidence == 1.0

    @pytest.mark.asyncio
    async def test_whitespace_answer(self, verifier):
        """纯空白答案应返回高置信。"""
        result = await verifier.verify("   \n  ", [])
        assert result.confidence == 1.0


class TestNumericConsistency:
    """数字一致性校验测试。"""

    @pytest.mark.asyncio
    async def test_numeric_supported(self, verifier):
        """答案数值在来源中找到相近值 → 无 unsupported_claim。"""
        answer = "今日金价 850 元。"
        sources = _make_sources(
            (1, "金价", "金价报 850 元", "https://news.sina.com.cn"),
        )
        result = await verifier.verify(answer, sources)
        assert result.unsupported_claims == []

    @pytest.mark.asyncio
    async def test_numeric_unsupported(self, verifier):
        """答案数值与来源偏差大 → unsupported_claim。"""
        answer = "今日金价 1000 元。"
        sources = _make_sources(
            (1, "金价", "金价报 850 元", "https://news.sina.com.cn"),
        )
        result = await verifier.verify(answer, sources)
        assert len(result.unsupported_claims) >= 1

    @pytest.mark.asyncio
    async def test_numeric_no_source(self, verifier):
        """来源无数值但答案有具体数值 → unsupported_claim。"""
        answer = "今日金价 850 元。"
        sources = _make_sources(
            (1, "金价", "金价上涨", "https://news.sina.com.cn"),
        )
        result = await verifier.verify(answer, sources)
        assert len(result.unsupported_claims) >= 1

    @pytest.mark.asyncio
    async def test_numeric_conflicting(self, verifier):
        """答案数值在 conflicting_values 中 → conflicting_claim。"""
        answer = "今日金价 850 元。"
        sources = _make_sources(
            (1, "金价A", "金价 850 元", "https://a.com"),
        )
        cross_data = {
            "validated_values": [],
            "single_source_values": [],
            "conflicting_values": [Decimal("850")],
        }
        result = await verifier.verify(answer, sources, cross_data)
        assert len(result.conflicting_claims) >= 1
        assert not result.is_consistent

    @pytest.mark.asyncio
    async def test_year_not_checked(self, verifier):
        """年份（2026）不应触发数值校验。"""
        answer = "2026 年金价上涨。"
        sources = _make_sources(
            (1, "金价", "金价上涨", "https://a.com"),
        )
        result = await verifier.verify(answer, sources)
        # 年份被过滤为噪音，不触发 unsupported_claim
        assert result.unsupported_claims == []


class TestDateConsistency:
    """日期一致性校验测试。"""

    @pytest.mark.asyncio
    async def test_date_in_source(self, verifier):
        """答案日期在来源中存在 → 无警告。"""
        answer = "2026 年金价上涨。"
        sources = _make_sources(
            (1, "金价", "2026 年金价数据", "https://a.com"),
        )
        result = await verifier.verify(answer, sources)
        date_warnings = [w for w in result.warnings if "日期" in w and "未在来源" in w]
        assert date_warnings == []

    @pytest.mark.asyncio
    async def test_date_not_in_source(self, verifier):
        """答案日期在来源中不存在 → 警告。"""
        answer = "2025 年金价上涨。"
        sources = _make_sources(
            (1, "金价", "2026 年金价数据", "https://a.com"),
        )
        result = await verifier.verify(answer, sources)
        date_warnings = [w for w in result.warnings if "2025" in w]
        assert len(date_warnings) >= 1

    @pytest.mark.asyncio
    async def test_relative_date_no_warning(self, verifier):
        """相对日期（今天/昨天）不应触发警告。"""
        answer = "今天金价上涨。"
        sources = _make_sources(
            (1, "金价", "金价数据", "https://a.com"),
        )
        result = await verifier.verify(answer, sources)
        date_warnings = [w for w in result.warnings if "今天" in w]
        assert date_warnings == []


class TestCitationValidity:
    """引用真实性校验测试。"""

    @pytest.mark.asyncio
    async def test_valid_citation(self, verifier):
        """有效引用 [1] → 无警告。"""
        answer = "金价 850 元[1]。"
        sources = _make_sources(
            (1, "金价", "850 元", "https://a.com"),
        )
        result = await verifier.verify(answer, sources)
        citation_warnings = [w for w in result.warnings if "超出有效来源范围" in w]
        assert citation_warnings == []

    @pytest.mark.asyncio
    async def test_invalid_citation(self, verifier):
        """超出范围的引用 [5] → 警告。"""
        answer = "金价 850 元[5]。"
        sources = _make_sources(
            (1, "金价", "850 元", "https://a.com"),
        )
        result = await verifier.verify(answer, sources)
        citation_warnings = [w for w in result.warnings if "[5]" in w]
        assert len(citation_warnings) >= 1

    @pytest.mark.asyncio
    async def test_question_mark_citation_no_warning(self, verifier):
        """[?] 引用不应触发范围警告。"""
        answer = "某声明[?]。"
        sources = _make_sources(
            (1, "金价", "850 元", "https://a.com"),
        )
        result = await verifier.verify(answer, sources)
        citation_warnings = [w for w in result.warnings if "超出有效来源范围" in w]
        assert citation_warnings == []


class TestCitationCoverage:
    """引用覆盖率计算测试。"""

    @pytest.mark.asyncio
    async def test_full_coverage(self, verifier):
        """所有事实句都有 [n] 引用 → 覆盖率 1.0。"""
        answer = "金价 850 元[1]。银价 30 元[2]。"
        sources = _make_sources(
            (1, "金价", "850", "https://a.com"),
            (2, "银价", "30", "https://b.com"),
        )
        result = await verifier.verify(answer, sources)
        assert result.citation_coverage == 1.0

    @pytest.mark.asyncio
    async def test_partial_coverage(self, verifier):
        """部分事实句有引用 → 覆盖率 0 < x < 1。"""
        answer = "金价 850 元[1]。银价 30 元。"
        sources = _make_sources(
            (1, "金价", "850", "https://a.com"),
        )
        result = await verifier.verify(answer, sources)
        assert 0.0 < result.citation_coverage < 1.0

    @pytest.mark.asyncio
    async def test_question_mark_not_counted(self, verifier):
        """[?] 不计入覆盖率分子。"""
        answer = "金价 850 元[?]。银价 30 元[1]。"
        sources = _make_sources(
            (1, "银价", "30", "https://a.com"),
        )
        result = await verifier.verify(answer, sources)
        # 2 个事实句，1 个有 [1] → 0.5
        assert result.citation_coverage == 0.5

    @pytest.mark.asyncio
    async def test_no_citation_zero_coverage(self, verifier):
        """无任何引用 → 覆盖率 0.0。"""
        answer = "金价 850 元。银价 30 元。"
        sources = _make_sources(
            (1, "金价", "850", "https://a.com"),
        )
        result = await verifier.verify(answer, sources)
        assert result.citation_coverage == 0.0


class TestCrossSourceConsistency:
    """多源交叉一致性计算测试。"""

    @pytest.mark.asyncio
    async def test_validated_value_high_consistency(self, verifier):
        """答案数值在 validated_values 中 → 高交叉一致性。"""
        answer = "金价 850 元。"
        sources = _make_sources(
            (1, "金价", "850", "https://a.com"),
        )
        cross_data = {
            "validated_values": [Decimal("850")],
            "single_source_values": [],
            "conflicting_values": [],
        }
        result = await verifier.verify(answer, sources, cross_data)
        assert result.cross_source_consistency == 1.0

    @pytest.mark.asyncio
    async def test_no_cross_data_zero_consistency(self, verifier):
        """无 cross_source_data → 交叉一致性 0.0。"""
        answer = "金价 850 元。"
        sources = _make_sources(
            (1, "金价", "850", "https://a.com"),
        )
        result = await verifier.verify(answer, sources, None)
        assert result.cross_source_consistency == 0.0

    @pytest.mark.asyncio
    async def test_value_not_validated_zero_consistency(self, verifier):
        """答案数值不在 validated_values 中 → 交叉一致性 0.0。"""
        answer = "金价 850 元。"
        sources = _make_sources(
            (1, "金价", "850", "https://a.com"),
        )
        cross_data = {
            "validated_values": [Decimal("999")],
            "single_source_values": [Decimal("850")],
            "conflicting_values": [],
        }
        result = await verifier.verify(answer, sources, cross_data)
        assert result.cross_source_consistency == 0.0


class TestSourceAuthority:
    """来源权威度计算测试。"""

    @pytest.mark.asyncio
    async def test_high_authority_source(self, verifier):
        """权威域名（gov.cn）应获得高权威度。"""
        sources = _make_sources(
            (1, "官方", "数据", "https://www.gov.cn/data"),
        )
        result = await verifier.verify("某声明。", sources)
        assert result.source_authority >= 0.9

    @pytest.mark.asyncio
    async def test_low_authority_source(self, verifier):
        """低质量域名应获得低权威度。"""
        sources = _make_sources(
            (1, "视频", "数据", "https://youtube.com/x"),
        )
        result = await verifier.verify("某声明。", sources)
        assert result.source_authority <= 0.3

    @pytest.mark.asyncio
    async def test_no_url_zero_authority(self, verifier):
        """无 URL 的来源 → 权威度 0.0。"""
        sources = [{"source_index": 1, "title": "x", "content": "y"}]
        result = await verifier.verify("某声明。", sources)
        assert result.source_authority == 0.0


class TestFreshnessScore:
    """来源新鲜度计算测试。"""

    @pytest.mark.asyncio
    async def test_fresh_content(self, verifier):
        """含当前年份和今日等关键词的内容 → 高新鲜度。"""
        from datetime import datetime, timezone
        year = datetime.now(timezone.utc).year
        sources = _make_sources(
            (1, "新闻", f"{year} 年今日最新数据 14:30 更新", "https://a.com"),
        )
        result = await verifier.verify("某声明。", sources)
        assert result.freshness_score >= 0.8

    @pytest.mark.asyncio
    async def test_stale_content(self, verifier):
        """旧年份内容 → 低新鲜度。"""
        sources = _make_sources(
            (1, "旧闻", "2020 年数据", "https://a.com"),
        )
        result = await verifier.verify("某声明。", sources)
        assert result.freshness_score <= 0.4


class TestConfidenceCalculation:
    """综合置信度计算测试。"""

    @pytest.mark.asyncio
    async def test_high_confidence(self, verifier):
        """多源验证 + 高引用覆盖 + 权威来源 + 新鲜 → 高置信度。"""
        from datetime import datetime, timezone
        year = datetime.now(timezone.utc).year
        answer = f"{year} 年今日金价 850 元[1]。"
        sources = _make_sources(
            (1, "金价", f"{year} 年今日金价 850 元", "https://www.gov.cn"),
        )
        cross_data = {
            "validated_values": [Decimal("850")],
            "single_source_values": [],
            "conflicting_values": [],
        }
        result = await verifier.verify(answer, sources, cross_data)
        assert result.confidence >= 0.7
        assert result.is_consistent is True

    @pytest.mark.asyncio
    async def test_low_confidence(self, verifier):
        """无引用 + 无交叉验证 + 低权威 → 低置信度。"""
        answer = "某数据 999 元。"
        sources = _make_sources(
            (1, "低质", "数据", "https://youtube.com"),
        )
        result = await verifier.verify(answer, sources, None)
        assert result.confidence < 0.4

    @pytest.mark.asyncio
    async def test_confidence_range(self, verifier):
        """置信度应在 0~1 范围内。"""
        answer = "金价 850 元[1]。"
        sources = _make_sources(
            (1, "金价", "850 元", "https://a.com"),
        )
        result = await verifier.verify(answer, sources)
        assert 0.0 <= result.confidence <= 1.0


class TestWarningSuffix:
    """警告后缀生成测试。"""

    def test_high_confidence_no_suffix(self, verifier):
        """高置信度 → 无警告后缀。"""
        result = VerificationResult(is_consistent=True, confidence=0.9)
        assert verifier.format_warning_suffix(result) == ""

    def test_medium_confidence_suffix(self, verifier):
        """中置信度 → 追加"部分信息"警告。"""
        result = VerificationResult(is_consistent=True, confidence=0.5)
        suffix = verifier.format_warning_suffix(result)
        assert "部分信息来源单一" in suffix

    def test_low_confidence_suffix(self, verifier):
        """低置信度 → 追加"可信度较低"警告。"""
        result = VerificationResult(is_consistent=False, confidence=0.2)
        suffix = verifier.format_warning_suffix(result)
        assert "可信度较低" in suffix


class TestIsConsistent:
    """is_consistent 判断测试。"""

    @pytest.mark.asyncio
    async def test_conflicting_makes_inconsistent(self, verifier):
        """有冲突声明 → is_consistent=False。"""
        answer = "金价 850 元。"
        sources = _make_sources(
            (1, "金价", "850", "https://a.com"),
        )
        cross_data = {
            "validated_values": [],
            "single_source_values": [],
            "conflicting_values": [Decimal("850")],
        }
        result = await verifier.verify(answer, sources, cross_data)
        assert result.is_consistent is False

    @pytest.mark.asyncio
    async def test_high_confidence_consistent(self, verifier):
        """高置信度无冲突 → is_consistent=True。"""
        from datetime import datetime, timezone
        year = datetime.now(timezone.utc).year
        answer = f"{year} 年金价 850 元[1]。"
        sources = _make_sources(
            (1, "金价", f"{year} 年 850 元", "https://www.gov.cn"),
        )
        cross_data = {
            "validated_values": [Decimal("850")],
            "single_source_values": [],
            "conflicting_values": [],
        }
        result = await verifier.verify(answer, sources, cross_data)
        assert result.is_consistent is True


class TestExtractFactSentences:
    """事实句提取测试。"""

    def test_format_lines_excluded(self, verifier):
        """格式行不应计入事实句。"""
        text = "### 标题\n---\n金价 850 元。\n"
        sentences = verifier._extract_fact_sentences(text)
        assert len(sentences) == 1
        assert "850" in sentences[0]

    def test_short_sentence_excluded(self, verifier):
        """过短句不应计入事实句。"""
        text = "好的。金价 850 元。"
        sentences = verifier._extract_fact_sentences(text)
        assert len(sentences) == 1

    def test_empty_text(self, verifier):
        """空文本 → 空列表。"""
        assert verifier._extract_fact_sentences("") == []
        assert verifier._extract_fact_sentences(None) == []  # type: ignore[arg-type]
