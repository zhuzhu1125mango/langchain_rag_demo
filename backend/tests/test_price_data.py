"""价格类实时数据相关模块单元测试。

覆盖 price_data_models、output_sanitizer、search_postprocessor 中的数值验证
与幻觉检测能力，以及 intent_router 对价格/汇率问题的路由判断。
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.services.intent_router import FallbackStrategy, IntentRouter, PrimaryMode
from src.services.output_sanitizer import NumericHallucinationDetector, OutputSanitizer
from src.services.search_postprocessor import SearchPostprocessor
from src.services.search_types import SearchResult
from src.services.tools.price_data_models import (
    ValidatedPriceResult,
    authority_score,
    compute_overall_confidence,
    cross_validate_numeric_values,
    format_price_answer,
    freshness_score,
)


class TestPriceDataModels:
    """价格数据模型与置信度计算测试。"""

    def test_freshness_score_now(self):
        """当前时间应获得最高分。"""
        now = datetime.now(timezone.utc)
        assert freshness_score(now, now=now) == 1.0

    def test_freshness_score_old(self):
        """超过一天的数据应获得低分。"""
        now = datetime.now(timezone.utc)
        past = now - timedelta(days=2)
        assert freshness_score(past, now=now) == 0.1

    def test_freshness_score_none(self):
        """无时间戳时给默认低分。"""
        assert freshness_score(None) == 0.3

    def test_authority_score_official(self):
        """官方来源应获得高权威性分。"""
        assert authority_score("sge.com.cn") == 0.95
        assert authority_score("lbma.org.uk") == 0.95

    def test_authority_score_financial_api(self):
        """知名金融数据 API 应获得较高分。"""
        assert authority_score("xaus.com") == 0.85
        assert authority_score("frankfurter.app") == 0.85

    def test_authority_score_unknown(self):
        """未知来源应获得低分。"""
        assert authority_score("random-blog.com") == 0.3

    def test_cross_validate_empty(self):
        """空列表交叉验证应返回零值与警告。"""
        value, score, warnings = cross_validate_numeric_values([])
        assert value == Decimal("0")
        assert score == 0.0
        assert "无可用数值" in warnings

    def test_cross_validate_single(self):
        """单一数据源应返回该值并提示单一数据源。"""
        value, score, warnings = cross_validate_numeric_values([Decimal("100")])
        assert value == Decimal("100")
        assert score == 0.7
        assert "仅单一数据源" in warnings

    def test_cross_validate_consistent(self):
        """偏差在容忍度内应获得较高一致性分。"""
        values = [Decimal("100"), Decimal("101"), Decimal("99")]
        _, score, warnings = cross_validate_numeric_values(values, tolerance=0.05)
        assert score > 0.7
        assert not warnings

    def test_cross_validate_inconsistent(self):
        """偏差过大应产生警告并降低一致性分。"""
        values = [Decimal("100"), Decimal("150")]
        _, score, warnings = cross_validate_numeric_values(values, tolerance=0.05)
        assert score < 0.5
        assert any("偏差" in w for w in warnings)

    def test_compute_overall_confidence(self):
        """综合置信度应在使用降级数据源时打折。"""
        base = compute_overall_confidence(
            consistency_score=1.0,
            freshness_score=1.0,
            authority_score=1.0,
            source_count=2,
            fallback_used=False,
        )
        fallback = compute_overall_confidence(
            consistency_score=1.0,
            freshness_score=1.0,
            authority_score=1.0,
            source_count=2,
            fallback_used=True,
        )
        assert fallback == round(base * 0.7, 2)

    def test_format_price_answer_high_confidence(self):
        """高置信度结果应直接给出数值。"""
        result = ValidatedPriceResult(
            value=Decimal("780"),
            currency="CNY",
            unit="gram",
            confidence=0.85,
            freshness_score=0.9,
            authority_score=0.85,
            cross_validation_score=0.9,
            sources=[],
        )
        answer = format_price_answer(result, item_name="黄金")
        assert "780" in answer
        assert "CNY/gram" in answer
        assert "85%" in answer

    def test_format_price_answer_very_low_confidence(self):
        """极低置信度不应给出具体数值。"""
        result = ValidatedPriceResult(
            value=Decimal("0"),
            currency="CNY",
            unit="gram",
            confidence=0.3,
            freshness_score=0.0,
            authority_score=0.0,
            cross_validation_score=0.0,
            sources=[],
        )
        answer = format_price_answer(result, item_name="黄金")
        assert "无法获取" in answer
        assert "780" not in answer


class TestNumericHallucinationDetector:
    """数字幻觉检测测试。"""

    def test_extract_numbers(self):
        """应能提取文本中的数值。"""
        text = "金价为 4,067.98 元/克，银价 25.3 美元/盎司。"
        numbers = NumericHallucinationDetector.extract_numbers(text)
        assert len(numbers) == 2
        assert numbers[0]["value"] == Decimal("4067.98")

    def test_detect_hallucination(self):
        """偏离参考值的数值应被标记为幻觉。"""
        answer = "今日金价为 4067.98 元/克。"
        reference = [Decimal("780")]
        has, warnings = NumericHallucinationDetector.detect(answer, reference, tolerance=0.05)
        assert has is True
        assert any("4067.98" in w for w in warnings)

    def test_detect_no_hallucination(self):
        """在容忍度内的数值不应被标记。"""
        answer = "今日金价约为 782 元/克。"
        reference = [Decimal("780")]
        has, warnings = NumericHallucinationDetector.detect(answer, reference, tolerance=0.05)
        assert has is False
        assert not warnings

    def test_detect_without_reference(self):
        """无参考数据时包含具体数值应被警告。"""
        answer = "今日金价为 800 元/克。"
        has, warnings = NumericHallucinationDetector.detect(answer, [])
        assert has is True
        assert any("幻觉" in w for w in warnings)


class TestOutputSanitizerNumeric:
    """输出清洗中数字幻觉相关测试。"""

    def test_sanitize_with_reference_numbers(self):
        """清洗时应检测数字幻觉并追加风险提示。"""
        text = "今日金价为 4067.98 元/克。"
        cleaned, polluted = OutputSanitizer.sanitize(
            text, reference_numbers=[Decimal("780")]
        )
        assert polluted is True
        assert "存在偏差" in cleaned

    def test_sanitize_no_hallucination(self):
        """数值一致时不应追加风险提示。"""
        text = "今日金价约为 780 元/克。"
        cleaned, polluted = OutputSanitizer.sanitize(
            text, reference_numbers=[Decimal("780")]
        )
        assert polluted is False
        assert "存在偏差" not in cleaned


class TestSearchPostprocessorNumeric:
    """搜索后处理数值交叉验证测试。"""

    @pytest.fixture
    def postprocessor(self):
        return SearchPostprocessor()

    def test_cross_validate_numeric_match(self, postprocessor):
        """与参考值接近的结果应标记为一致。"""
        results = [
            SearchResult(title="t", url="http://a.com", content="金价 780 元/克"),
        ]
        validated, consistency = postprocessor.cross_validate_numeric(
            results, reference_values=[Decimal("780")], tolerance=0.05
        )
        assert "与权威数据源一致" in validated[0].content
        assert consistency == 1.0

    def test_cross_validate_numeric_mismatch(self, postprocessor):
        """与参考值偏差大的结果应标记为不一致。"""
        results = [
            SearchResult(title="t", url="http://a.com", content="金价 4067 元/克"),
        ]
        validated, consistency = postprocessor.cross_validate_numeric(
            results, reference_values=[Decimal("780")], tolerance=0.05
        )
        assert "存在偏差" in validated[0].content
        assert consistency == 0.0

    def test_extract_numeric_values(self, postprocessor):
        """应正确提取文本中的候选数值（公开方法返回结构化字典）。"""
        items = postprocessor.extract_numeric_values("价格为 1,234.5 元")
        values = [item["value"] for item in items]
        assert Decimal("1234.5") in values


class TestIntentRouterPrice:
    """意图路由对价格/汇率问题的判断测试。"""

    @pytest.fixture
    def router(self):
        return IntentRouter()

    def test_is_price_question(self, router):
        """应识别各类贵金属价格问法。"""
        assert router.is_price_question("今日金价") is True
        assert router.is_price_question("白银价格") is True
        assert router.is_price_question("北京天气") is False

    def test_is_exchange_rate_question(self, router):
        """应识别各类汇率/兑换问法。"""
        assert router.is_exchange_rate_question("美元兑人民币汇率") is True
        assert router.is_exchange_rate_question("100美元换人民币") is True
        assert router.is_exchange_rate_question("100 USD to CNY") is True
        assert router.is_exchange_rate_question("今天金价") is False

    @pytest.mark.asyncio
    async def test_route_price_tool_first(self, router):
        """金价问题应优先使用金价工具。"""
        decision = await router.route("今天黄金价格是多少")
        assert decision.primary_mode == PrimaryMode.TOOL_FIRST
        assert "gold_price" in decision.suggested_tools
        assert decision.needs_realtime is True
        assert decision.fallback_strategy == FallbackStrategy.WEB_SEARCH

    @pytest.mark.asyncio
    async def test_route_exchange_rate_tool_first(self, router):
        """汇率问题应优先使用汇率工具，优先于通用计算。"""
        decision = await router.route("美元兑人民币汇率")
        assert decision.primary_mode == PrimaryMode.TOOL_FIRST
        assert "exchange_rate" in decision.suggested_tools
        assert decision.needs_realtime is True

    @pytest.mark.asyncio
    async def test_route_exchange_rate_over_calculation(self, router):
        """汇率问题不应被计算器工具误接。"""
        decision = await router.route("100美元等于多少人民币")
        assert "exchange_rate" in decision.suggested_tools
        assert "calculator" not in decision.suggested_tools
