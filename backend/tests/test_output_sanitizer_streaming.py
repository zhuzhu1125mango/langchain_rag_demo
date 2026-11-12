"""流式输出的清洗边界：不能吃掉片段之间的空格与换行。

这组用例对应一个真实的生产缺陷（2026-11-12）：`OutputSanitizer.sanitize` 原本
在结尾无条件 `strip()`，而它在流式路径上是**逐 chunk** 调用的。chunk 的边界经常
正好落在空格上，于是每个片段两端的空格被剥掉，全文粘连：

    docker logs backend-prod 2>&1 | grep -E  →  dockerlogsbackend-prod2>&1|grep-E

中文看不出问题，代码块则完全不可复制。落库的 `final_answer` 也是粘连的，
所以「答案里有没有提到某个事实」这类指标也被它带偏（事实串带空格就匹配不上）。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.output_sanitizer import OutputSanitizer  # noqa: E402


class TestStreamChunksPreserveSpaces:
    def test_join_of_sanitized_chunks_equals_original(self):
        """回归：逐片清洗后拼接，必须与原文逐字节相同。"""
        chunks = [
            "请执行",
            " docker logs",
            " backend-prod",
            " 2>&1 ",
            "| grep -E",
            ' "一致性"',
        ]
        joined = "".join(
            OutputSanitizer.sanitize(c, strip_edges=False)[0] for c in chunks
        )
        assert joined == "".join(chunks)

    def test_trailing_newline_survives(self):
        """片段以换行结尾时不能把换行吃掉——否则整段会黏成一行。"""
        assert OutputSanitizer.sanitize("第一步。\n", strip_edges=False)[0] == "第一步。\n"

    def test_indented_code_line_survives(self):
        """代码块里的缩进是语义，不能被当空白剥掉。"""
        line = "    docker compose up -d\n"
        assert OutputSanitizer.sanitize(line, strip_edges=False)[0] == line

    def test_pure_space_chunk_is_kept(self):
        assert OutputSanitizer.sanitize(" ", strip_edges=False)[0] == " "

    def test_chunk_without_edges_is_unchanged(self):
        assert OutputSanitizer.sanitize("rag_request_total", strip_edges=False)[0] == "rag_request_total"


class TestWholeTextStillNormalized:
    def test_default_still_strips(self):
        """整段调用保持原行为，避免改变非流式路径的输出。"""
        assert OutputSanitizer.sanitize("  answer  ")[0] == "answer"

    def test_default_strips_newlines_at_edges(self):
        assert OutputSanitizer.sanitize("\n\n答案\n\n")[0] == "答案"


class TestPollutionStillFiltered:
    def test_thinking_tags_removed_in_stream_mode(self):
        out = OutputSanitizer.sanitize("<thinking>想一下</thinking>结论", strip_edges=False)[0]
        assert "thinking" not in out
        assert "结论" in out

    def test_tool_call_line_removed_in_stream_mode(self):
        """片段里混入工具调用行时仍要过滤——这是清洗的主要职责。"""
        out, polluted = OutputSanitizer.sanitize(
            '{"name": "web_search", "arguments": {"q": "x"}}', strip_edges=False
        )
        assert out.strip() == ""
        assert polluted is True

    def test_tool_narration_marks_polluted(self):
        out, polluted = OutputSanitizer.sanitize("调用 web_search({...})", strip_edges=False)
        assert polluted is True

    def test_filtering_lines_keeps_other_lines_intact(self):
        text = "正常第一行\n调用 web_search({...})\n正常第三行\n"
        out, polluted = OutputSanitizer.sanitize(text, strip_edges=False)
        assert polluted is True
        assert out.startswith("正常第一行\n")
        assert "正常第三行\n" in out
        assert "web_search" not in out
