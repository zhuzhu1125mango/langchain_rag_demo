"""整节回填判据采集脚本的纯函数（2026-11-25）。

这个脚本的作用是把「0.4 这条线只有一道题撑着」变成可复现的采集：小节父块分数、
各阈值下的上下文规模、以及答案覆盖率。真正决定结论对不对的是三处口径，所以只钉这三处：

1. **覆盖率**对大小写/空白/标点不敏感——文档写 `rag:semcache:*`，答案写
   「清空 rag:semcache 缓存」，不该因为冒号星号判成没命中；
2. **父块分数只取原始命中块**——邻块/整节块继承父块分数，让它们参与取最大值
   不改变结果，但「命中几块」会被放大（一节看着命中 5 块，其实只有 1 块带进 4 个邻块）；
3. **汇总只平均评估过的题**——没跑答案的题按 0 计入，会让「题少」看起来像「答得差」。
"""

import json
import os
import sys

import pytest
from langchain_core.documents import Document

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.collect_section_scores import (  # noqa: E402
    InputError,
    aggregate_runs,
    fact_coverage,
    load_questions,
    normalize,
    section_table,
    summarize,
)


def _doc(idx, score, heading, text="正文", **extra):
    return Document(
        page_content=text,
        metadata={
            "document_id": "d1",
            "chunk_index": idx,
            "heading_path": heading,
            "rerank_score": score,
            **extra,
        },
    )


class TestCoverage:
    def test_punctuation_and_case_insensitive(self):
        cov = fact_coverage("清空 RAG:SEMCACHE 缓存；核对 embedding_dimension", ["rag:semcache"])
        assert cov["ratio"] == 1.0
        assert cov["missing"] == []

    def test_missing_fact_is_reported(self):
        cov = fact_coverage("只有 restore.sh 这一条", ["restore.sh", "停写", "P95"])
        assert cov["ratio"] == pytest.approx(1 / 3, rel=1e-3)
        assert set(cov["missing"]) == {"停写", "P95"}

    def test_empty_facts_is_not_a_zero_score(self):
        # 没有 facts 时 ratio=0 但 matched/missing 都空：调用方据此跳过评判，
        # 而不是把它当成「答错了」。
        cov = fact_coverage("任意答案", [])
        assert cov == {"ratio": 0.0, "matched": [], "missing": []}

    def test_blank_fact_never_matches(self):
        # 空串在 normalize 后为空，`"" in flat` 恒真——不排除就会白送一条命中
        assert fact_coverage("答案", ["  "])["ratio"] == 0.0

    def test_normalize_keeps_cjk_and_digits(self):
        assert normalize("4.1 恢复顺序（第 7~8 步）") == "41恢复顺序第78步"


class TestLoadQuestions:
    def _write(self, tmp_path, payload):
        p = tmp_path / "q.json"
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return str(p)

    def test_file_level_kb_ids_and_thresholds(self, tmp_path):
        path = self._write(
            tmp_path,
            {
                "kb_ids": ["kb1"],
                "thresholds": [0.5, 0.3, 0.5],
                "questions": [{"question": "怎么恢复？", "facts": ["restore.sh"]}],
            },
        )
        questions, kb, thresholds = load_questions(path, [], [])
        assert questions[0]["kb_ids"] == ["kb1"]
        assert questions[0]["facts"] == ["restore.sh"]
        assert kb == ["kb1"]
        assert thresholds == [0.3, 0.5]  # 去重且升序

    def test_question_level_kb_ids_win(self, tmp_path):
        path = self._write(
            tmp_path,
            {"kb_ids": ["kb1"], "questions": [{"question": "q", "kb_ids": ["kb2"]}]},
        )
        questions, _, _ = load_questions(path, [], ["kb-cli"])
        assert questions[0]["kb_ids"] == ["kb2"]

    def test_cli_questions_append(self, tmp_path):
        path = self._write(tmp_path, {"kb_ids": ["kb1"], "questions": [{"question": "a"}]})
        questions, _, _ = load_questions(path, ["b"], [])
        assert [q["question"] for q in questions] == ["a", "b"]

    def test_bare_list_payload(self, tmp_path):
        path = self._write(tmp_path, [{"question": "a"}])
        questions, _, _ = load_questions(path, [], ["kb1"])
        assert questions[0]["kb_ids"] == ["kb1"]

    def test_missing_question_rejected(self, tmp_path):
        path = self._write(tmp_path, {"questions": [{"facts": ["x"]}]})
        with pytest.raises(InputError, match="question"):
            load_questions(path, [], ["kb1"])

    def test_threshold_out_of_range_rejected(self, tmp_path):
        path = self._write(tmp_path, {"thresholds": [1.5], "questions": [{"question": "a"}]})
        with pytest.raises(InputError, match="0~1"):
            load_questions(path, [], ["kb1"])

    def test_no_questions_rejected(self):
        with pytest.raises(InputError, match="没有题目"):
            load_questions(None, [], ["kb1"])


class TestSectionTable:
    def test_groups_by_h2_section_and_takes_max_score(self):
        # 4.1 与 4.2 同属「4. 恢复」这一节：分桶键是 H2，父块分数取节内最高重排分。
        # 这不是实现细节，而是 min_section_score 的作用单位——它筛的是整节。
        docs = [
            _doc(24, 0.50, "部署 > 4. 恢复 > 4.1 恢复顺序"),
            _doc(25, 0.42, "部署 > 4. 恢复 > 4.1 恢复顺序"),
            _doc(28, 0.34, "部署 > 4. 恢复 > 4.2 恢复后必做"),
        ]
        rows, unclassified = section_table(docs)
        assert unclassified == 0
        assert len(rows) == 1
        row = rows[0]
        assert row["prefix"] == "部署 > 4. 恢复"
        assert row["title"] == "4. 恢复"  # 不是 4.1/4.2 的名字
        assert row["score"] == 0.50
        assert row["hits"] == [24, 25, 28]
        assert row["children"] == ["4.1 恢复顺序", "4.2 恢复后必做"]

    def test_two_sections_are_ordered_by_score(self):
        docs = [
            _doc(10, 0.31, "部署 > 5. 回滚 > 5.1 决策矩阵"),
            _doc(24, 0.50, "部署 > 4. 恢复 > 4.1 恢复顺序"),
        ]
        rows, _ = section_table(docs)
        assert [r["title"] for r in rows] == ["4. 恢复", "5. 回滚"]

    def test_inherited_blocks_do_not_inflate_original_count(self):
        docs = [
            _doc(25, 0.50, "部署 > 4. 恢复 > 4.1 恢复顺序"),
            _doc(24, 0.50, "部署 > 4. 恢复 > 4.1 恢复顺序", neighbor_expand=True),
            _doc(26, 0.50, "部署 > 4. 恢复 > 4.1 恢复顺序", section_expand=True),
        ]
        rows, _ = section_table(docs)
        assert rows[0]["originals"] == 1
        assert rows[0]["inherited"] == 2
        assert rows[0]["score"] == 0.50

    def test_two_level_heading_is_unclassified(self):
        # 只有 H1 > H2 时 section_prefix 返回空串：这类块不属于任何「二级小节」，
        # 整节回填也不会碰它们，必须计入 unclassified 而不是硬塞进某一节。
        rows, unclassified = section_table([_doc(1, 0.9, "部署 > 4. 恢复")])
        assert rows == []
        assert unclassified == 1


class TestAggregateRuns:
    """`--rounds` 的聚合语义：均值而非末次，且不稳定的 fact 要被看见。"""

    def _run(self, cov, matched, missing, chars=100):
        return {
            "coverage": cov,
            "matched": matched,
            "missing": missing,
            "answer_len": chars,
            "elapsed_s": 10.0,
        }

    def test_mean_not_last_run(self):
        agg = aggregate_runs(
            [
                self._run(0.67, ["a", "b"], ["c"]),
                self._run(0.67, ["a", "b"], ["c"]),
                self._run(1.0, ["a", "b", "c"], []),
            ],
            ["a", "b", "c"],
        )
        # 末次是 1.0；均值才是 0.78
        assert agg["coverage"] == pytest.approx(0.78, abs=1e-3)
        assert agg["coverage_runs"] == [0.67, 0.67, 1.0]
        assert agg["coverage_min"] == 0.67 and agg["coverage_max"] == 1.0

    def test_unstable_fact_reported(self):
        agg = aggregate_runs(
            [self._run(0.5, ["a"], ["b"]), self._run(1.0, ["a", "b"], [])],
            ["a", "b"],
        )
        # b 在 2 次里命中 1 次：这种边缘点最该被看见，而不是按末次判成「命中」
        assert agg["fact_hit_rate"]["b"] == 0.5
        assert agg["unstable_facts"] == ["b"]

    def test_stable_facts_not_flagged(self):
        agg = aggregate_runs(
            [self._run(0.5, ["a"], ["b"]), self._run(0.5, ["a"], ["b"])],
            ["a", "b"],
        )
        assert agg["unstable_facts"] == []
        assert agg["fact_hit_rate"] == {"a": 1.0, "b": 0.0}

    def test_empty_runs_rejected(self):
        with pytest.raises(ValueError):
            aggregate_runs([], ["a"])


class TestSummarize:
    def _record(self, sims):
        return {"simulations": sims}

    def test_averages_only_answered_questions(self):
        records = [
            self._record({0.4: {"chunks": 6, "chars": 2000, "coverage": 0.8}}),
            self._record({0.4: {"chunks": 10, "chars": 4000, "coverage": None}}),
        ]
        row = summarize(records, [0.4])[0]
        assert row["questions"] == 2
        assert row["answered"] == 1
        assert row["avg_coverage"] == 0.8  # 不是 0.4
        assert row["avg_chars"] == 3000

    def test_missing_threshold_is_skipped(self):
        row = summarize([self._record({0.3: {"chunks": 4, "chars": 900}})], [0.3, 0.9])[1]
        assert row["questions"] == 0
        assert row["avg_coverage"] is None
