"""search_postprocessor 单元测试。

覆盖扩展后的公开数值提取、多源交叉验证、ScoredResult 扩展字段等场景。
"""

from decimal import Decimal

import pytest

from src.services.search_postprocessor import ScoredResult, SearchPostprocessor
from src.services.search_types import SearchResult


@pytest.fixture
def postprocessor() -> SearchPostprocessor:
    """后处理器实例。"""
    return SearchPostprocessor()


def _make_result(title: str, url: str, content: str, source: str = "web", engine: str = "searxng") -> SearchResult:
    """构造测试用 SearchResult。"""
    return SearchResult(
        title=title,
        url=url,
        content=content,
        source=source,
        engine=engine,
    )


class TestExtractNumericValues:
    """公开数值提取方法测试。"""

    def test_empty_text(self, postprocessor):
        """空文本应返回空列表。"""
        assert postprocessor.extract_numeric_values("") == []
        assert postprocessor.extract_numeric_values(None) == []  # type: ignore[arg-type]

    def test_plain_number(self, postprocessor):
        """应提取普通整数和小数。"""
        items = postprocessor.extract_numeric_values("价格 4067.98 元")
        assert len(items) == 1
        assert items[0]["value"] == Decimal("4067.98")
        assert items[0]["raw"] == "4067.98"

    def test_thousands_separator(self, postprocessor):
        """应处理千分位逗号。"""
        items = postprocessor.extract_numeric_values("1,234.5 元")
        assert items[0]["value"] == Decimal("1234.5")
        assert items[0]["raw"] == "1,234.5"

    def test_currency_symbol(self, postprocessor):
        """应处理货币符号前缀。"""
        items = postprocessor.extract_numeric_values("¥850 / $1200")
        values = [i["value"] for i in items]
        assert Decimal("850") in values
        assert Decimal("1200") in values

    def test_context_field(self, postprocessor):
        """应返回数值周围的上下文。"""
        text = "今日黄金价格 4067.98 元/克 创新高"
        items = postprocessor.extract_numeric_values(text)
        assert len(items) == 1
        ctx = items[0]["context"]
        assert "4067.98" in ctx
        assert "黄金" in ctx or "价格" in ctx

    def test_position_field(self, postprocessor):
        """应返回数值在原文中的起始位置。"""
        text = "abc 123 def"
        items = postprocessor.extract_numeric_values(text)
        assert items[0]["position"] == 4

    def test_multiple_numbers(self, postprocessor):
        """应提取多个数值。"""
        items = postprocessor.extract_numeric_values("金价 4067 元，银价 30.5 元")
        assert len(items) >= 2


class TestCrossSourceValidate:
    """多源数值交叉验证测试。"""

    def test_empty_results(self, postprocessor):
        """空结果应返回空的三分类。"""
        data = postprocessor.cross_source_validate([])
        assert data["validated_values"] == []
        assert data["single_source_values"] == []
        assert data["conflicting_values"] == []

    def test_validated_values(self, postprocessor):
        """≥2 个独立域名出现相近数值 → validated。"""
        results = [
            _make_result(
                "金价A",
                "https://news.sina.com.cn/gold",
                "今日金价 850.5 元/克",
            ),
            _make_result(
                "金价B",
                "https://news.qq.com/finance",
                "黄金价格报 851 元/克",
            ),
        ]
        data = postprocessor.cross_source_validate(results)
        # 850.5 和 851 偏差 < 5%，且来自两个独立域名 → validated
        assert len(data["validated_values"]) >= 1
        assert data["conflicting_values"] == []

    def test_single_source_values(self, postprocessor):
        """仅 1 个来源的数值 → single_source。"""
        results = [
            _make_result(
                "独家数据",
                "https://example.com/unique",
                "某特殊指标 12345 点",
            ),
        ]
        data = postprocessor.cross_source_validate(results)
        # 只有 1 个来源，应为 single_source
        assert len(data["single_source_values"]) >= 1
        assert data["validated_values"] == []

    def test_conflicting_values(self, postprocessor):
        """多源但偏差 > 5% 的数值应被分类到 single_source 或冲突分类。

        注：偏差过大的数值不会聚到同一簇，因此会被分到 single_source。
        真正的 conflicting 需要同聚类内偏差 > tolerance，此处验证分类逻辑稳定。
        """
        results = [
            _make_result(
                "来源A",
                "https://site-a.com",
                "价格 100 元",
            ),
            _make_result(
                "来源B",
                "https://site-b.com",
                "价格 200 元",
            ),
        ]
        data = postprocessor.cross_source_validate(results)
        # 100 和 200 偏差 100% > 5%，不会聚到一起，各自成为 single_source
        assert data["validated_values"] == []
        assert len(data["single_source_values"]) >= 2

    def test_same_domain_not_validated(self, postprocessor):
        """同一域名的两个相近数值不应计入 validated。"""
        results = [
            _make_result(
                "金价A",
                "https://news.sina.com.cn/page1",
                "金价 850 元",
            ),
            _make_result(
                "金价B",
                "https://news.sina.com.cn/page2",
                "金价 851 元",
            ),
        ]
        data = postprocessor.cross_source_validate(results)
        # 同一域名 sina.com.cn，不应算作多源验证
        assert data["validated_values"] == []

    def test_noise_year_filtered(self, postprocessor):
        """年份（如 2026）应被过滤为噪音，不参与交叉验证。"""
        results = [
            _make_result(
                "新闻A",
                "https://site-a.com",
                "2026 年金价 850 元",
            ),
            _make_result(
                "新闻B",
                "https://site-b.com",
                "2026 年金价 851 元",
            ),
        ]
        data = postprocessor.cross_source_validate(results)
        # 2026 被过滤，850 和 851 来自两个域名 → validated
        assert len(data["validated_values"]) >= 1


class TestScoredResultFields:
    """ScoredResult 扩展字段测试。"""

    def test_source_id_assigned(self, postprocessor):
        """score 方法应为每个结果分配 1-based source_id。"""
        results = [
            _make_result("r1", "https://a.com", "内容1"),
            _make_result("r2", "https://b.com", "内容2"),
            _make_result("r3", "https://c.com", "内容3"),
        ]
        scored = postprocessor.score(results)
        assert scored[0].source_id == 1
        assert scored[1].source_id == 2
        assert scored[2].source_id == 3

    def test_domain_extracted(self, postprocessor):
        """score 方法应填充归一化域名（去除 www.）。"""
        results = [
            _make_result("r1", "https://www.example.com/page", "内容1"),
            _make_result("r2", "https://news.sina.com.cn/finance", "内容2"),
        ]
        scored = postprocessor.score(results)
        assert scored[0].domain == "example.com"
        assert scored[1].domain == "news.sina.com.cn"

    def test_default_fields(self):
        """ScoredResult 默认值应为 source_id=0, domain=''。"""
        r = _make_result("r", "https://x.com", "c")
        sr = ScoredResult(result=r)
        assert sr.source_id == 0
        assert sr.domain == ""


class TestExtractDomain:
    """域名提取辅助方法测试。"""

    def test_normal_url(self, postprocessor):
        assert postprocessor._extract_domain("https://www.example.com/path") == "example.com"

    def test_no_www(self, postprocessor):
        assert postprocessor._extract_domain("https://news.sina.com.cn/x") == "news.sina.com.cn"

    def test_uppercase_normalized(self, postprocessor):
        assert postprocessor._extract_domain("https://EXAMPLE.COM/X") == "example.com"

    def test_invalid_url(self, postprocessor):
        assert postprocessor._extract_domain("not a url") == ""


class TestIsNoiseNumber:
    """噪音数值过滤测试。"""

    def test_year_is_noise(self, postprocessor):
        assert postprocessor._is_noise_number(Decimal("2026"), "2026 年") is True

    def test_normal_price_not_noise(self, postprocessor):
        assert postprocessor._is_noise_number(Decimal("850.5"), "金价 850.5 元") is False

    def test_tiny_value_is_noise(self, postprocessor):
        assert postprocessor._is_noise_number(Decimal("0.05"), "比率 0.05") is True

    def test_version_context_is_noise(self, postprocessor):
        assert postprocessor._is_noise_number(Decimal("3"), "version 3 发布") is True


class TestBackwardCompatibility:
    """向后兼容性测试：cross_validate_numeric 应继续工作。"""

    def test_cross_validate_numeric_still_works(self, postprocessor):
        """cross_validate_numeric 应基于新的 extract_numeric_values 正常工作。"""
        results = [
            _make_result("金价", "https://a.com", "金价 850 元"),
        ]
        validated, consistency = postprocessor.cross_validate_numeric(
            results, reference_values=[Decimal("850")], tolerance=0.05
        )
        assert consistency == 1.0
        assert "一致" in validated[0].content

    def test_cross_validate_numeric_no_match(self, postprocessor):
        """参考值不匹配时应标记偏差。"""
        results = [
            _make_result("金价", "https://a.com", "金价 850 元"),
        ]
        validated, consistency = postprocessor.cross_validate_numeric(
            results, reference_values=[Decimal("1000")], tolerance=0.05
        )
        assert consistency == 0.0
        assert "偏差" in validated[0].content


class TestFreshnessNoDeprecation:
    """时效性评分不应触发 DeprecationWarning。"""

    def test_freshness_no_warning(self, postprocessor, recwarn):
        """_freshness_score 应使用 timezone-aware datetime，无弃用警告。"""
        postprocessor._freshness_score("2026 年今日数据")
        # 过滤出 DeprecationWarning
        dep_warnings = [w for w in recwarn.list if issubclass(w.category, DeprecationWarning)]
        assert len(dep_warnings) == 0
