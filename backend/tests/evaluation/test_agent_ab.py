"""Agent 演进 A/B 评估测试（设计文档 agent-ab-evaluation.md）。

- 离线用例（默认跑，CI）：agent 数据集 kb/hybrid 子集在 BM25 检索栈上
  指标达 EVAL_MIN_* 阈值（KB 检索口径回归卡点）。
- e2e 用例（--run-e2e）：读取 run_agent_ab.py --mode live 生成的 A/B 明细，
  执行门槛断言（§6）。文件缺失时跳过并提示先跑两臂。
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from run_agent_ab import (  # noqa: E402
    DEFAULT_CORPUS,
    DEFAULT_DATASET,
    DEFAULT_RUN_A,
    DEFAULT_RUN_B,
    compare_runs,
    compare_plan_runs,
)
from run_eval import (  # noqa: E402
    evaluate,
    get_thresholds,
    load_jsonl,
)


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_agent_ab_offline_kb_non_degradation():
    """离线：kb/hybrid 子集 BM25 检索指标达阈值（KB 口径回归卡点）。"""
    dataset = load_jsonl(DEFAULT_DATASET)
    corpus = load_jsonl(DEFAULT_CORPUS)
    sub = [s for s in dataset if s["category"] in ("kb", "hybrid")]
    assert sub, "agent 数据集 kb/hybrid 子集为空"

    result = evaluate(sub, corpus, top_k=5)
    metrics = result["metrics"]
    for name, threshold in get_thresholds().items():
        assert metrics[name] >= threshold, (
            f"Agent 数据集 KB 检索回归: {name}={metrics[name]:.3f} < 阈值 {threshold}"
        )


def test_agent_ab_dataset_shape():
    """数据集结构校验：类别分布与 golden 引用合法性。"""
    dataset = load_jsonl(DEFAULT_DATASET)
    corpus = load_jsonl(DEFAULT_CORPUS)
    corpus_ids = {d["id"] for d in corpus}
    cats = {}
    for sample in dataset:
        assert sample.get("id") and sample.get("question")
        cats[sample["category"]] = cats.get(sample["category"], 0) + 1
        for g in sample.get("golden_documents", []):
            assert g in corpus_ids, f"{sample['id']} golden 文档 {g} 不在 eval_corpus 中"
    assert cats.get("kb", 0) >= 5 and cats.get("web", 0) >= 5
    assert cats.get("hybrid", 0) >= 5 and cats.get("tool", 0) >= 3
    assert cats.get("fast_path", 0) >= 1
    assert cats.get("multi", 0) >= 3, "L2 分层规划 multi 子集不应为空"


def _synthetic_multi_run(values):
    """构造 multi 子集 synthetic run：values 为 [(faithfulness, relevance, total_ms, steps)]。"""
    samples = []
    for i, (fth, rel, ms, steps) in enumerate(values, start=1):
        samples.append({
            "category": "multi", "id": f"multi_{i}", "question": f"q{i}",
            "answer": "答" * 10, "error": None, "judge_ok": True,
            "faithfulness": fth, "relevance": rel, "total_ms": ms, "agent_steps": steps,
        })
    return {"arm": "B", "samples": samples}


def test_compare_plan_non_degrade_and_p95_slack():
    """L2 验收纯函数：plan_on 不显著劣化 faithfulness、P95 增量超时判 FAIL。"""
    dataset = load_jsonl(DEFAULT_DATASET)
    off = _synthetic_multi_run([(0.9, 1.0, 5000, 2), (0.8, 0.9, 6000, 3), (0.7, 0.8, 4000, 2)])
    # plan_on：faithfulness 略高一点（规划收益），P95 增量远超 5s → P95 门 FAIL，质量门 PASS
    on = _synthetic_multi_run([(0.92, 1.0, 9000, 2), (0.85, 0.9, 9500, 3), (0.72, 0.8, 8500, 2)])
    rep = compare_plan_runs(dataset, off, on, plan_off_label="plan_off", plan_on_label="plan_on", category="multi")
    by_name = {c["name"]: c["ok"] for c in rep["checks"]}
    assert by_name["multi_faithfulness_non_degrade"] is True
    assert by_name["multi_relevance_non_degrade"] is True
    # P95=9000-6000=+3000ms ≤5s → PASS（本例构造未超时）
    assert by_name["multi_plan_p95_increase"] is True

    # 反例：P95 增量超 5s → FAIL
    on_bad = _synthetic_multi_run([(0.92, 1.0, 30000, 2), (0.85, 0.9, 31000, 3), (0.72, 0.8, 29000, 2)])
    rep_bad = compare_plan_runs(dataset, off, on_bad, plan_off_label="plan_off", plan_on_label="plan_on", category="multi")
    bad_by_name = {c["name"]: c["ok"] for c in rep_bad["checks"]}
    assert bad_by_name["multi_plan_p95_increase"] is False

    # 反例：faithfulness 显著劣化 → FAIL
    on_low = _synthetic_multi_run([(0.5, 1.0, 5000, 2), (0.4, 0.9, 6000, 3), (0.5, 0.8, 4000, 2)])
    rep_low = compare_plan_runs(dataset, off, on_low, plan_off_label="plan_off", plan_on_label="plan_on", category="multi")
    low_by_name = {c["name"]: c["ok"] for c in rep_low["checks"]}
    assert low_by_name["multi_faithfulness_non_degrade"] is False


@pytest.mark.e2e
def test_agent_ab_live_compare_thresholds():
    """e2e：A/B 真实链路门槛断言（§6）。

    前置：分别运行
        uv run python scripts/run_agent_ab.py --mode live --arm A
        uv run python scripts/run_agent_ab.py --mode live --arm B
    """
    if not DEFAULT_RUN_A.exists() or not DEFAULT_RUN_B.exists():
        pytest.skip(
            "缺少 A/B 明细（backend/agent_ab_run_A.json / agent_ab_run_B.json），"
            "先运行 scripts/run_agent_ab.py --mode live --arm A|B"
        )
    dataset = load_jsonl(DEFAULT_DATASET)
    run_a = _load_json(DEFAULT_RUN_A)
    run_b = _load_json(DEFAULT_RUN_B)
    report = compare_runs(dataset, run_a, run_b)
    failures = [c for c in report["checks"] if not c["ok"]]
    assert not failures, f"A/B 门槛未达标: {failures}"
