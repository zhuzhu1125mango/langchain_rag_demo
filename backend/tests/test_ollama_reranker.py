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
            ("", 0.0),
            ("not a number", 0.0),
            ("75", 0.75),  # 百分制输入兼容
            ("15", 0.15),  # 大于 10 的异常值按百分制归一化
        ],
    )
    def test_extract_score(self, text, expected):
        assert OllamaReranker._extract_score(text) == pytest.approx(expected)
