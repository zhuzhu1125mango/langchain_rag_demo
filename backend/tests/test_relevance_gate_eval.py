"""相关性门禁的两个面：判定逻辑与「拒答留痕」（2026-11-10）。

背景：线上门禁把「最终选中的片段」按 `rerank_score` 与阈值比，全低于阈值就清空
上下文、按「知识库里没有相关内容」回答。2026-11-10 想给它加「低置信兜底」，
离线一验发现**分数分不出对错**（失败题 top1 从 0.12 到 0.97，答对的题里有 0.019），
所以这里只钉两件仍然成立的事：

1. 门禁判定与生产实现一致（`any >= t or avg >= t`），别让评估脚本与线上跑偏；
2. 拒答必须留痕 —— 分数、差多少、丢掉的是哪一块。没有这条日志，
   「拒了多少、拒掉的是不是本该能答」永远查不出来。
"""

import logging
import os
import sys

from langchain_core.documents import Document

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.eval_relevance_gate import (  # noqa: E402
    evaluate,
    gate_decision,
    safe_thresholds,
)
from src.services.pipeline.kb_stage import (  # noqa: E402
    log_gate_suppression,
    summarize_gate_suppression,
)


def _doc(score, text="片段内容"):
    return Document(page_content=text, metadata={"rerank_score": score})


def _sample(question, scores, coverage):
    return {"question": question, "kind": "detail", "scores": scores, "coverage": coverage}


class TestGateDecision:
    def test_passes_when_any_score_reaches_threshold(self):
        assert gate_decision([0.31, 0.02], 0.3) is True

    def test_refuses_when_all_below_threshold(self):
        assert gate_decision([0.29, 0.02], 0.3) is False

    def test_average_can_pass_even_if_no_single_score_does(self):
        """实现里保留了 `avg >= t` 分支，评估脚本必须跟着它，否则两边结论会漂。"""
        assert gate_decision([0.28, 0.28], 0.3) is False
        assert gate_decision([0.31, 0.29], 0.3) is True

    def test_empty_is_refused(self):
        assert gate_decision([], 0.3) is False


class TestEvaluateCounts:
    def test_counts_false_refusal_and_missed(self):
        samples = [
            _sample("答得好但分低", [0.12, 0.01], 1.0),   # 误拒
            _sample("该拒且分了低", [0.05, 0.01], 0.0),   # 正确拒答
            _sample("答得差但分高", [0.97, 0.96], 0.0),   # 漏拒
            _sample("正常", [0.9, 0.5], 1.0),             # 正常放行
        ]
        r = evaluate(samples, 0.3)
        assert (r["refused"], r["false_refuse"], r["missed"], r["answered"]) == (2, 1, 1, 2)
        assert r["false_refuse_questions"] == ["答得好但分低"]

    def test_zero_threshold_refuses_nothing(self):
        samples = [_sample("a", [0.0], 0.0)]
        r = evaluate(samples, 0.0)
        assert r["refused"] == 0 and r["missed"] == 1


class TestSafeThresholds:
    def test_picks_max_refusal_among_zero_false_refusal(self):
        results = [
            {"threshold": 0.0, "refused": 0, "false_refuse": 0, "missed": 2},
            {"threshold": 0.2, "refused": 1, "false_refuse": 0, "missed": 1},
            {"threshold": 0.5, "refused": 2, "false_refuse": 1, "missed": 0},
        ]
        assert safe_thresholds(results) == [0.2]

    def test_reports_nothing_when_every_threshold_misfires(self):
        results = [{"threshold": 0.5, "refused": 3, "false_refuse": 2, "missed": 0}]
        assert safe_thresholds(results) == []


class TestSummarizeGateSuppression:
    def test_captures_scores_and_gap(self):
        docs = [_doc(0.12, "系统由这些部分组成"), _doc(0.009), _doc(0.001)]
        s = summarize_gate_suppression(docs, 0.3)
        assert s["chunks"] == 3
        assert s["max_score"] == 0.12
        assert s["avg_score"] == 0.0433
        assert s["gap_to_threshold"] == 0.18

    def test_preview_is_single_line_and_bounded(self):
        s = summarize_gate_suppression([_doc(0.1, "第一行\n第二行" + "x" * 200)], 0.3)
        assert "\n" not in s["top1_preview"]
        assert len(s["top1_preview"]) <= 80

    def test_empty_docs_does_not_crash(self):
        s = summarize_gate_suppression([], 0.3)
        assert (s["chunks"], s["max_score"], s["avg_score"], s["top1_preview"]) == (0, 0.0, 0.0, "")

    def test_missing_metadata_score_counts_as_zero(self):
        doc = Document(page_content="无分数", metadata={})
        assert summarize_gate_suppression([doc], 0.3)["max_score"] == 0.0


class TestGateSuppressionIsLogged:
    """拒答必须留下可核对的日志——否则「能答却拒答」在生产上不可见、不可数。"""

    def test_emits_reportable_log_with_scores(self, caplog):
        docs = [_doc(0.12, "系统由这些部分组成"), _doc(0.009), _doc(0.001)]
        with caplog.at_level(logging.ERROR, logger="rag_system"):
            payload = log_gate_suppression(docs, 0.3)

        assert payload["max_score"] == 0.12
        messages = [r.getMessage() for r in caplog.records if "relevance_gate_suppressed" in r.getMessage()]
        assert messages, "门禁清空上下文时必须记一条日志"
        msg = messages[-1]
        assert "severity=reportable" in msg
        assert "max_score=0.12" in msg and "threshold=0.3" in msg
        assert "系统由这些部分组成" in msg, "被丢掉的那块开头要能看见，否则无法判断本该不该拒"
