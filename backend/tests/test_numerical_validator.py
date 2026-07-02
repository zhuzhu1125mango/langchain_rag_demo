"""多源数值交叉验证器单元测试。"""

import pytest

from src.services.numerical_validator import NumericalValidator


@pytest.fixture
def validator():
    return NumericalValidator()


class TestNumericalExtraction:
    """数值事实提取测试。"""

    def test_extract_simple_number(self, validator):
        facts = validator.extract_facts("价格为 100 元")
        assert len(facts) == 1
        assert facts[0].value == pytest.approx(100.0)
        assert facts[0].unit == "元"

    def test_extract_wan_unit(self, validator):
        facts = validator.extract_facts("销售额为 500 万元")
        assert len(facts) == 1
        assert facts[0].value == pytest.approx(5_000_000.0)

    def test_extract_yi_unit(self, validator):
        facts = validator.extract_facts("GDP 为 3.2 亿美元")
        assert len(facts) == 1
        assert facts[0].value == pytest.approx(320_000_000.0)

    def test_extract_thousand(self, validator):
        facts = validator.extract_facts("人口约 5000 人")
        assert len(facts) == 1
        assert facts[0].value == pytest.approx(5000.0)

    def test_extract_metric_inference(self, validator):
        facts = validator.extract_facts("北京今天气温 25 度")
        assert len(facts) >= 1
        assert any(f.metric == "温度" for f in facts)


class TestNumericalValidation:
    """多源交叉验证测试。"""

    def test_consistent_sources(self, validator):
        sources = [
            {"content": "金价为 780 元/克", "source_index": 1},
            {"content": "黄金价格 780 元每克", "source_index": 2},
        ]
        result = validator.validate(sources)
        assert result.is_consistent is True
        assert result.confidence == 1.0
        assert len(result.conflicts) == 0

    def test_conflict_detected(self, validator):
        sources = [
            {"content": "金价为 780 元/克", "source_index": 1},
            {"content": "金价为 900 元/克", "source_index": 2},
        ]
        result = validator.validate(sources)
        assert result.is_consistent is False
        assert len(result.conflicts) >= 1
        assert result.confidence < 1.0

    def test_conflict_severity_high(self, validator):
        sources = [
            {"content": "温度为 20 摄氏度", "source_index": 1},
            {"content": "温度为 80 摄氏度", "source_index": 2},
        ]
        result = validator.validate(sources)
        assert any(c.severity == "high" for c in result.conflicts)

    def test_unit_normalization_conflict(self, validator):
        sources = [
            {"content": "收入为 5000 元", "source_index": 1},
            {"content": "收入为 0.5 万元", "source_index": 2},
        ]
        result = validator.validate(sources)
        # 5000 元 == 0.5 万元，应无冲突
        assert result.is_consistent is True

    def test_warning_format(self, validator):
        sources = [
            {"content": "金价为 780 元/克", "source_index": 1},
            {"content": "金价为 900 元/克", "source_index": 2},
        ]
        result = validator.validate(sources)
        warning = validator.format_warning(result)
        assert "数据一致性提示" in warning
        assert "来源1" in warning
        assert "来源2" in warning

    def test_single_source_no_conflict(self, validator):
        sources = [{"content": "人口为 1000 人", "source_index": 1}]
        result = validator.validate(sources)
        assert result.is_consistent is True

    def test_empty_sources(self, validator):
        result = validator.validate([])
        assert result.is_consistent is True
        assert result.confidence == 1.0


class TestDisplayFormatting:
    """数值显示格式化测试。"""

    def test_format_wan(self, validator):
        from src.services.numerical_validator import NumericalFact
        fact = NumericalFact(value=50000, unit="元", metric="价格", original_text="5万")
        text = validator._format_display_value(fact)
        assert "5.00 万元" in text

    def test_format_yi(self, validator):
        from src.services.numerical_validator import NumericalFact
        fact = NumericalFact(value=100000000, unit="美元", metric="GDP", original_text="1亿")
        text = validator._format_display_value(fact)
        assert "1.00 亿美元" in text
