"""答案覆盖率复核口径的测试。

这套口径的用途是**把测量误差和真问题分开**，所以它自己必须先被钉住：
放宽太多会让所有题都「命中」（指标失去意义），放宽太少则继续把措辞差异
当成漏写去优化（给测量误差写代码）。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.analyze_answer_gap import fact_hit_loose, fact_hit_strict, score  # noqa: E402


class TestStrict:
    def test_exact_substring(self):
        assert fact_hit_strict("reload", "改完用 POST /-/reload 生效") is True

    def test_alternates(self):
        assert fact_hit_strict("部分恢复|单知识库", "支持单知识库恢复") is True

    def test_miss(self):
        assert fact_hit_strict("reload", "需要重启 Prometheus") is False


class TestLoose:
    def test_latin_tokens_split_by_particle(self):
        """「alembic downgrade」被写成「alembic 的 downgrade」——真答案，严格口径判漏。"""
        answer = "回滚时可以使用 alembic 的 downgrade 指令回到上一个版本"
        assert fact_hit_strict("alembic downgrade", answer) is False
        assert fact_hit_loose("alembic downgrade", answer) is True

    def test_whitespace_and_case(self):
        assert fact_hit_loose("docker compose", "执行 DOCKER  COMPOSE up -d") is True

    def test_chinese_phrase_is_not_loosened_to_characters(self):
        """纯中文短语不做逐字放宽：否则「不可打乱」会被「不」「打」这类单字蒙中。"""
        assert fact_hit_loose("不可打乱", "必须严格按照指定顺序执行") is False

    def test_chinese_with_extra_particle_still_misses(self):
        """已知局限：中文多一个「的」就判漏，这是子串口径的固有限制，如实记录。"""
        assert fact_hit_loose("恢复顺序", "恢复的顺序不能打乱") is False

    def test_does_not_turn_a_wrong_answer_into_a_hit(self):
        """把 reload 答成 restart 是**答错**，放宽口径不能把它判成命中。"""
        assert fact_hit_loose("promtool", "直接重启 Prometheus 容器即可") is False
        assert fact_hit_loose("reload", "需要重启 Prometheus 才能生效") is False


class TestScore:
    def test_reports_hits_and_misses(self):
        frac, hits, miss = score("用 promtool 校验后 reload", ["promtool", "reload", "grafana"], False)
        assert frac == 2 / 3
        assert hits == ["promtool", "reload"]
        assert miss == ["grafana"]

    def test_empty_facts(self):
        assert score("x", [], False)[0] == 0.0
