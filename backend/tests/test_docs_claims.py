"""文档「无口径判断」检查（#34）。

为什么测：这条 lint 一旦误报多了就会被无视，一旦漏报就等于没有。
所以两个方向都要钉：该抓的抓（"性能足够"）、不该抓的不抓
（"成本高，raw 兜底可接受"这种纯取舍，以及脚本自己在定规矩的那句）。
"""

import importlib.util
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "check_docs_claims.py"

_spec = importlib.util.spec_from_file_location("check_docs_claims", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


class TestFindIssues:
    def test_flags_bare_performance_claim(self):
        issues = mod.find_issues("查询性能足够，不需要优化。\n")
        assert issues and issues[0][1] == "性能足够"

    def test_flags_scale_plus_subjective(self):
        issues = mod.find_issues("无索引，万级会话下可接受。\n")
        assert issues, "这正是历史上那条假声称"
        assert "规模判断" in issues[0][2]

    def test_untested_marker_passes(self):
        assert mod.find_issues("万级会话下的响应（未测）：预计 100ms 量级。\n") == []

    def test_measured_wording_passes(self):
        assert mod.find_issues("实测 p95=120ms，见 docs/benchmarks/perf-baseline.md\n") == []

    def test_plain_tradeoff_word_passes(self):
        """「可接受」单独出现是取舍表达，不是规模判断。"""
        assert mod.find_issues("- 成本高，raw 兜底可接受，列为候选。\n") == []

    def test_rule_statement_passes(self):
        """写作约定页自己会写「不许用『性能足够』这类词」，不能被判红。"""
        assert mod.find_issues("**不许**用「可接受 / 性能足够」这类词代替测量。\n") == []

    def test_code_fence_is_skipped(self):
        text = "```\n# 示例：性能足够\n```\n"
        assert mod.find_issues(text) == []

    def test_line_numbers_are_accurate(self):
        text = "第一行\n第二行 性能足够\n"
        issues = mod.find_issues(text)
        assert issues[0][0] == 2


class TestRepoDocs:
    def test_repository_docs_are_clean(self):
        """仓库当下必须干净，否则 CI 一接就是红的。"""
        assert mod.main(["--paths", "README.md", "docs"]) == 0

    def test_audit_dir_is_skipped_by_default(self):
        """审计报告要引用问题原文，默认不扫；显式传文件才扫。"""
        paths = mod.iter_markdown([_REPO_ROOT / "docs"])
        assert not any("audit" in p.parts for p in paths)
