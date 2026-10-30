"""OllamaReranker 单元测试。"""

import pytest

from src.services.ollama_reranker import OllamaReranker


class TestExtractScore:
    """测试分数解析逻辑。"""

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("7.5", 0.75),
            ("10", 1.0),
            ("0", 0.0),
            ("5", 0.5),
            ("Score: 8.2", 0.82),
            ("The relevance is 9 out of 10.", 0.9),
            ("75", 0.75),  # 百分制输入兼容
            ("15", 0.15),  # 大于 10 的异常值按百分制归一化
        ],
    )
    def test_extract_score(self, text, expected):
        assert OllamaReranker._extract_score(text) == pytest.approx(expected)

    @pytest.mark.parametrize("text", ["", "   ", "not a number", "N/A"])
    def test_unparseable_returns_none_not_zero(self, text):
        """解析失败必须是 None：0.0 会被下游当成「判定不相关」而滤掉候选。"""
        assert OllamaReranker._extract_score(text) is None
