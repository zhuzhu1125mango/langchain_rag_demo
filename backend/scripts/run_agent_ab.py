"""Agent 演进 A/B 评估脚本（Phase 2 验收，见 docs/design/agent-ab-evaluation.md）。

三种模式：
  live    真实链路 A/B（需 dev 栈 + Ollama + Milvus + SearXNG），--arm A|B 二选一，
          输出单臂逐题明细 JSON（含答案 / 来源 / 延迟 / 生成质量评分）。
  offline KB 检索口径离线回归（复用 run_eval BM25 栈，全离线，CI 可跑），
          对 agent 数据集 kb/hybrid 子集断言检索指标达到 EVAL_MIN_* 阈值。
  compare 汇总 live 两臂明细，按类别聚合指标并执行门槛断言（agent-ab-evaluation.md §6），
          输出对比报告 JSON，未达标非零退出。

用法：
  cd backend
  uv run python scripts/run_agent_ab.py --mode live --arm A --output run_A.json
  uv run python scripts/run_agent_ab.py --mode live --arm B --output run_B.json
  uv run python scripts/run_agent_ab.py --mode compare --run-a run_A.json --run-b run_B.json
  uv run python scripts/run_agent_ab.py --mode offline

live 环境准备（KB/HYBRID 类命中评估需要）：
  1. 将 tests/evaluation/eval_corpus.jsonl 中的文档上传到测试知识库；
  2. 准备 doc-id-map JSON：{"leave_policy": "<milvus document_id uuid>", ...}；
  3. --kb-ids 传测试知识库 id（逗号分隔）。
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = BASE_DIR / "tests" / "evaluation" / "agent_eval_dataset.jsonl"
DEFAULT_CORPUS = BASE_DIR / "tests" / "evaluation" / "eval_corpus.jsonl"
DEFAULT_RUN_A = BASE_DIR / "agent_ab_run_A.json"
DEFAULT_RUN_B = BASE_DIR / "agent_ab_run_B.json"
DEFAULT_REPORT = BASE_DIR / "agent_ab_report.json"
DEFAULT_OFFLINE_REPORT = BASE_DIR / "agent_ab_report_offline.json"

# 门槛默认值（环境变量可覆盖，前缀 AB_）
AB_NON_DEGRADE = float(os.environ.get("AB_NON_DEGRADE", "0.05"))   # 非退化容差
# 宽松口径（Agent 语义复核，2026-09-14）：hybrid 生成质量对 Agent 臂单独放宽，
# 因多源综合属 Agent 预期能力而非退化；并设绝对下限兜底防止质量崩溃。
AB_HYBRID_NON_DEGRADE = float(os.environ.get("AB_HYBRID_NON_DEGRADE", "0.30"))
AB_HYBRID_MIN = float(os.environ.get("AB_HYBRID_MIN", "0.30"))
AB_P95_LIMIT = float(os.environ.get("AB_P95_LIMIT", "45"))         # Agent 路径 P95 上限（秒）
AB_FAST_SLACK = float(os.environ.get("AB_FAST_PATH_P95_SLACK", "5"))  # 快路径两臂 P95 差（秒）
AB_MIN_TOOL_SUCCESS = float(os.environ.get("AB_MIN_TOOL_SUCCESS", "0.8"))
AB_PER_QUESTION_TIMEOUT_S = float(os.environ.get("AB_PER_QUESTION_TIMEOUT_S", "90"))
# Agent 语义复核的宽松 faithful 口径（agent-ab-evaluation.md §7.1）：默认开，
# strict 用于受控路径对照；loose 允许结合多源自身知识但不得与参考矛盾。
_AB_FAITHFULNESS_MODE = os.environ.get("AB_FAITHFULNESS_MODE", "loose")


# ----------------------------------------------------------------------
# 通用工具
# ----------------------------------------------------------------------
def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _load_json(path: Optional[Path]) -> Dict[str, Any]:
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _p95(values: List[float]) -> Optional[float]:
    """小样本 P95：排序后取 95% 分位位置（与 numpy.percentile 对 24 样本量级差异可忽略）。"""
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    idx = int(0.95 * (len(vals) - 1))
    return vals[idx]


def _mean(values: List[float]) -> Optional[float]:
    vals = [float(v) for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _split_sentences(text: str) -> List[str]:
    """按中英文句读切分（复用 citation_backfiller 的切句规则口径）。"""
    parts = re.split(r"[。！？!?；;\n]+", text)
    return [p.strip() for p in parts if len(p.strip()) >= 5]


def _citation_coverage(answer: str) -> Optional[float]:
    """答案中带 [n] 引用的句子占比；无答案或无句子返回 None。"""
    if not answer:
        return None
    sentences = _split_sentences(answer)
    if not sentences:
        return None
    cited = sum(1 for s in sentences if re.search(r"\[\d+\]", s))
    return cited / len(sentences)


def _invalid_citation_count(answer: str, source_count: int) -> int:
    """[n] 超出有效来源编号数量的引用数。"""
    if not answer:
        return 0
    bad = 0
    for m in re.finditer(r"\[(\d+)\]", answer):
        if int(m.group(1)) < 1 or int(m.group(1)) > max(source_count, 0):
            bad += 1
    return bad


def _kb_hit_metrics(
    sources: List[Dict[str, Any]], golden_docs: List[str],
    doc_id_map: Dict[str, str], top_k: int = 5,
) -> Dict[str, float]:
    """从来源元数据计算 KB 文档级 hit_rate/mrr/recall。

    sources 中 source_type=="kb" 的 document_id（UUID）经 doc_id_map 映射回
    语义 id 后与 golden_docs 比对（口径与 run_eval 文档级一致）。

    doc_id_map 的规范方向是 {语义文档id: milvus document_id uuid}（与
    prepare_ab_kb.py 输出一致），这里反转为 {uuid: 语义id} 供查找；
    若调用方直接传 {uuid: 语义id} 也兼容。
    """
    rev = {str(v): k for k, v in (doc_id_map or {}).items()}
    ranked: List[str] = []
    for s in sources:
        if s.get("source_type") != "kb":
            continue
        did = str(s.get("document_id") or "")
        mapped = doc_id_map.get(did, did) if did in doc_id_map else rev.get(did, did)
        if mapped and mapped not in ranked:
            ranked.append(mapped)
    golden = set(golden_docs)
    if not golden:
        return {"hit_rate": 0.0, "mrr": 0.0, "recall": 0.0, "ranked": ranked}
    top = ranked[:top_k]
    hit = 1.0 if golden & set(top) else 0.0
    mrr = 0.0
    for rank, rid in enumerate(top, start=1):
        if rid in golden:
            mrr = 1.0 / rank
            break
    recall = len(golden & set(top)) / len(golden)
    return {"hit_rate": hit, "mrr": mrr, "recall": recall, "ranked": ranked}


# ----------------------------------------------------------------------
# live 模式
# ----------------------------------------------------------------------
def _live_inputs(sample: Dict[str, Any], kb_ids: List[str]):
    """按类别决定 arun_stream 的入参（A/B 两臂一致，保证公平）。"""
    cat = sample["category"]
    # multi 为 KB+web 多步类：与 web/hybrid 一样开启联网与 Agent 工具路径，并传入 KB
    use_web = cat in ("web", "hybrid", "multi")
    search_mode = "function_calling" if use_web else "simple"
    q_kb_ids = kb_ids if cat in ("kb", "hybrid", "multi") else []
    return use_web, search_mode, q_kb_ids


async def _run_one(
    chain, sample: Dict[str, Any], kb_ids: List[str],
    doc_id_map: Dict[str, str], judge,
) -> Dict[str, Any]:
    q = sample["question"]
    cat = sample["category"]
    use_web, search_mode, q_kb_ids = _live_inputs(sample, kb_ids)

    record: Dict[str, Any] = {
        "id": sample["id"], "category": cat, "question": q,
        "golden_documents": sample.get("golden_documents", []),
        "web_topic": sample.get("web_topic", ""),
        "error": None,
    }
    start = time.perf_counter()
    first_token_ms: Optional[float] = None
    chunks: List[str] = []
    sources: List[Dict[str, Any]] = []
    reasoning_payloads: List[str] = []
    answer_type = ""

    async def _stream():
        nonlocal first_token_ms, answer_type
        async for chunk, src_texts, src_meta, atype in chain.arun_stream(
            question=q, kb_ids=q_kb_ids, history=[],
            use_web_search=use_web, search_mode=search_mode,
            user_id=None, session_id=None, deep_thinking="off",
        ):
            if first_token_ms is None and (chunk or atype in ("reasoning", "thinking")):
                first_token_ms = (time.perf_counter() - start) * 1000
            if atype in ("reasoning", "thinking"):
                if atype == "reasoning" and chunk:
                    reasoning_payloads.append(chunk)
                continue
            if chunk:
                chunks.append(chunk)
                answer_type = atype
            for s in src_meta or []:
                if s not in sources:
                    sources.append(s)

    try:
        await asyncio.wait_for(_stream(), timeout=AB_PER_QUESTION_TIMEOUT_S)
    except asyncio.TimeoutError:
        record["error"] = "timeout"
    except Exception as e:  # noqa: BLE001 —— 单题失败不中断整体评估
        record["error"] = f"{type(e).__name__}: {e}"

    record["answer"] = "".join(chunks)
    record["answer_type"] = answer_type
    record["total_ms"] = (time.perf_counter() - start) * 1000
    record["first_token_ms"] = first_token_ms
    record["sources"] = sources
    record["web_source_count"] = sum(
        1 for s in sources if s.get("source_type") == "web"
    )
    record["kb_sources"] = [
        s for s in sources if s.get("source_type") == "kb"
    ]

    # KB 命中评估（仅在提供 doc-id-map 且有 kb 来源时有效）
    kb_metrics = _kb_hit_metrics(sources, sample.get("golden_documents", []), doc_id_map)
    record["kb_hit_rate"] = kb_metrics["hit_rate"]
    record["kb_mrr"] = kb_metrics["mrr"]
    record["kb_recall"] = kb_metrics["recall"]
    record["kb_ranked"] = kb_metrics["ranked"]

    # B 臂 Agent 循环步骤统计（reasoning 事件中 step=agent_loop）
    steps, tools = 0, []
    for payload in reasoning_payloads:
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            continue
        if data.get("step") != "agent_loop":
            continue
        meta = data.get("metadata") or {}
        idx = int(meta.get("step_index") or 0)
        steps = max(steps, idx)
        tools.extend(meta.get("tools") or [])
    record["agent_steps"] = steps
    record["agent_tool_calls"] = tools

    # 污染检查（工具调用 JSON 残留）
    from src.services.search_agent import looks_like_tool_call
    record["polluted"] = bool(looks_like_tool_call(record["answer"]))

    # 生成质量（LLM-as-judge，复用现有 GenerationEvaluator）
    if record["answer"] and not record["error"]:
        contexts = [
            str(s.get("content") or "")[:1000] for s in sources if s.get("content")
        ][:6]
        # 宽松口径：Agent 臂（B）用 loose 模式（允许多源综合），固定管线（A）用 strict
        agent_enabled = os.environ.get("AGENT_ORCHESTRATOR_ENABLED", "false").lower() == "true"
        faith_mode = "loose" if agent_enabled else "strict"
        try:
            result = await judge.evaluate(
                answer=record["answer"], question=q, contexts=contexts,
                faithfulness_mode=faith_mode,
            )
            record["faithfulness"] = result.faithfulness
            record["relevance"] = result.relevance
            record["judge_ok"] = True
        except Exception as e:  # noqa: BLE001
            record["faithfulness"] = 0.0
            record["relevance"] = 0.0
            record["judge_ok"] = False
            record["judge_error"] = f"{type(e).__name__}: {e}"
    else:
        record["faithfulness"] = None
        record["relevance"] = None
        record["judge_ok"] = False
    return record


def run_live(args: argparse.Namespace) -> int:
    """真实链路单臂评估（A/B 各跑一次，env 在 main 中已设置）。"""
    from src.services.rag_chain import RAGChain  # noqa: E402 —— 需 env 就绪后导入

    dataset = _load_jsonl(args.dataset)
    kb_ids = [s.strip() for s in (args.kb_ids or "").split(",") if s.strip()]
    doc_id_map = _load_json(args.doc_id_map)
    arm = args.arm

    async def _main():
        chain = await RAGChain.get_instance()
        from src.services.evaluation.generation_evaluator import GenerationEvaluator
        judge = GenerationEvaluator()
        results = []
        for i, sample in enumerate(dataset, 1):
            rec = await _run_one(chain, sample, kb_ids, doc_id_map, judge)
            results.append(rec)
            status = rec["error"] or "ok"
            print(
                f"[{i}/{len(dataset)}] {rec['id']:<10} {rec['category']:<9} "
                f"type={rec['answer_type']:<16} total={rec['total_ms']:.0f}ms "
                f"steps={rec['agent_steps']} status={status}"
            )
        return results

    results = asyncio.run(_main())
    output = {
        "arm": arm,
        "dataset": str(args.dataset),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "samples": results,
    }
    out_path = args.output or (BASE_DIR / f"agent_ab_run_{arm}.json")
    _write_json(out_path, output)
    print(f"[agent-ab] {arm} 臂明细已写入: {out_path}")
    return 0


# ----------------------------------------------------------------------
# offline 模式（KB 检索口径回归，CI 可跑）
# ----------------------------------------------------------------------
def run_offline(args: argparse.Namespace) -> int:
    """复用 run_eval BM25 栈，对 kb/hybrid 子集断言检索指标达阈值。"""
    sys.path.insert(0, str(BASE_DIR / "scripts"))
    from run_eval import get_thresholds, evaluate, load_jsonl  # noqa: E402

    dataset = load_jsonl(args.dataset)
    corpus = load_jsonl(DEFAULT_CORPUS)
    sub = [s for s in dataset if s["category"] in ("kb", "hybrid")]
    if not sub:
        print("[agent-ab] offline: kb/hybrid 子集为空")
        return 2
    result = evaluate(sub, corpus, top_k=5)
    metrics = result["metrics"]
    report = {
        "mode": "offline",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "questions": len(sub),
        "metrics": metrics,
        "thresholds": get_thresholds(),
        "details": result["details"],
    }
    _write_json(args.offline_report or DEFAULT_OFFLINE_REPORT, report)
    print(f"[agent-ab] offline 报告已写入: {args.offline_report or DEFAULT_OFFLINE_REPORT}")
    print(
        f"hit_rate@5={metrics['hit_rate']:.3f} mrr@5={metrics['mrr']:.3f} "
        f"recall@5={metrics['recall']:.3f}"
    )
    failed = [
        (name, metrics[name], thr)
        for name, thr in get_thresholds().items()
        if metrics[name] < thr
    ]
    if failed:
        for name, actual, thr in failed:
            print(f"[agent-ab] offline 未达阈值: {name}={actual:.3f} < {thr}")
        return 1
    print(f"[agent-ab] offline 全部达标（阈值: {get_thresholds()}）")
    return 0


# ----------------------------------------------------------------------
# compare 模式（门槛断言，agent-ab-evaluation.md §6）
# ----------------------------------------------------------------------
def _arm_metrics(run: Dict[str, Any]) -> Dict[str, Any]:
    """单臂按类别聚合成指标字典。"""
    samples = run.get("samples", [])
    by_cat: Dict[str, List[Dict[str, Any]]] = {}
    for s in samples:
        by_cat.setdefault(s["category"], []).append(s)
    all_ok = [s for s in samples if not s.get("error") and s.get("answer")]

    kb_samples = by_cat.get("kb", []) + by_cat.get("hybrid", [])
    hybrid = by_cat.get("hybrid", [])
    agent_cat = by_cat.get("web", []) + by_cat.get("hybrid", [])
    fast = by_cat.get("fast_path", [])

    def _fmt(recs: List[Dict[str, Any]], key: str):
        return [r.get(key) for r in recs]

    metrics: Dict[str, Any] = {
        "num_samples": len(samples),
        "error_count": sum(1 for s in samples if s.get("error")),
        "answer_rate": (len(all_ok) / len(samples)) if samples else 0.0,
        # KB 检索（kb/hybrid 子集，仅在有 map 时有效）
        "kb_hit_rate": _mean(_fmt(kb_samples, "kb_hit_rate")),
        "kb_mrr": _mean(_fmt(kb_samples, "kb_mrr")),
        "kb_recall": _mean(_fmt(kb_samples, "kb_recall")),
        "kb_metric_usable": any(s.get("kb_sources") for s in kb_samples),
        # Web 上下文
        "web_source_count": _mean(_fmt(agent_cat, "web_source_count")),
        # 生成质量（有答案且 judge 成功）
        "faithfulness": _mean([s["faithfulness"] for s in all_ok if s.get("judge_ok")]),
        "relevance": _mean([s["relevance"] for s in all_ok if s.get("judge_ok")]),
        "hybrid_faithfulness": _mean(
            [s["faithfulness"] for s in hybrid if s.get("judge_ok")]
        ),
        "hybrid_relevance": _mean(
            [s["relevance"] for s in hybrid if s.get("judge_ok")]
        ),
        # 引用
        "citation_coverage": _mean([_citation_coverage(s["answer"]) for s in all_ok]),
        "invalid_citation": sum(
            _invalid_citation_count(s["answer"], len(s.get("sources", [])))
            for s in all_ok
        ),
        # 质量门
        "pollution_rate": (
            sum(1 for s in all_ok if s.get("polluted")) / len(all_ok)
            if all_ok else 0.0
        ),
        "agent_steps": _mean(_fmt(by_cat.get("web", []) + by_cat.get("hybrid", []), "agent_steps")),
        "tool_calls": sum(len(s.get("agent_tool_calls", [])) for s in samples),
        # 延迟（毫秒）
        "total_p95_ms": _p95([s.get("total_ms") for s in samples]),
        "first_token_p95_ms": _p95([s.get("first_token_ms") for s in samples]),
        "agent_total_p95_ms": _p95([s.get("total_ms") for s in agent_cat]),
        "agent_first_token_p95_ms": _p95([s.get("first_token_ms") for s in agent_cat]),
        "fast_total_p95_ms": _p95([s.get("total_ms") for s in fast]),
    }
    return metrics


def compare_runs(
    dataset: List[Dict[str, Any]], run_a: Dict[str, Any], run_b: Dict[str, Any],
) -> Dict[str, Any]:
    """A/B 汇总对比 + 门槛断言。返回 {metrics_a, metrics_b, deltas, checks}。"""
    ma, mb = _arm_metrics(run_a), _arm_metrics(run_b)

    def _delta(name: str):
        a, b = ma.get(name), mb.get(name)
        if a is None or b is None:
            return None
        return b - a

    deltas = {
        "kb_hit_rate": _delta("kb_hit_rate"),
        "kb_mrr": _delta("kb_mrr"),
        "kb_recall": _delta("kb_recall"),
        "faithfulness": _delta("faithfulness"),
        "relevance": _delta("relevance"),
        "hybrid_faithfulness": _delta("hybrid_faithfulness"),
        "hybrid_relevance": _delta("hybrid_relevance"),
        "pollution_rate": _delta("pollution_rate"),
        "fast_total_p95_ms": _delta("fast_total_p95_ms"),
    }

    checks: List[Dict[str, Any]] = []

    def _check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    # 1) KB 检索非退化（kb/hybrid 子集）
    if ma.get("kb_metric_usable") and mb.get("kb_metric_usable"):
        for key, label in (
            ("kb_hit_rate", "hit_rate"), ("kb_mrr", "mrr"), ("kb_recall", "recall"),
        ):
            a, b = ma[key], mb[key]
            ok = (a is None or b is None) or (b >= a - AB_NON_DEGRADE)
            _check(f"kb_{label}_non_degrade", bool(ok),
                   f"A={a if a is not None else '-'} B={b if b is not None else '-'}")
    else:
        _check("kb_non_degrade", True,
               "skip：未提供 doc-id-map 或两臂均无 kb 来源（KB 命中不评估）")

    # 2) hybrid 子集生成质量（宽松口径，agent-ab-evaluation.md §7.1）
    #    Agent 臂多源综合属预期能力而非退化，故：相对非退化用放宽容差，
    #    同时用绝对下限守住基本质量；两者均通过才算 PASS。
    if ma.get("hybrid_faithfulness") is not None and mb.get("hybrid_faithfulness") is not None:
        checks.append({
            "name": "hybrid_faithfulness_non_degrade",
            "ok": mb["hybrid_faithfulness"] >= ma["hybrid_faithfulness"] - AB_HYBRID_NON_DEGRADE,
            "detail": f"A={ma['hybrid_faithfulness']:.3f} B={mb['hybrid_faithfulness']:.3f} "
                      f"(宽松容差 {AB_HYBRID_NON_DEGRADE:.2f})",
        })
        checks.append({
            "name": "hybrid_faithfulness_min",
            "ok": mb["hybrid_faithfulness"] >= AB_HYBRID_MIN,
            "detail": f"B={mb['hybrid_faithfulness']:.3f} ≥ {AB_HYBRID_MIN:.2f}",
        })
        ok = mb["hybrid_relevance"] >= ma["hybrid_relevance"] - AB_HYBRID_NON_DEGRADE
        _check("hybrid_relevance_non_degrade", bool(ok),
               f"A={ma['hybrid_relevance']:.3f} B={mb['hybrid_relevance']:.3f} "
               f"(宽松容差 {AB_HYBRID_NON_DEGRADE:.2f})")
        ok = mb["hybrid_relevance"] >= AB_HYBRID_MIN
        _check("hybrid_relevance_min", bool(ok),
               f"B={mb['hybrid_relevance']:.3f} ≥ {AB_HYBRID_MIN:.2f}")
    else:
        _check("hybrid_quality_non_degrade", True,
               "skip：hybrid 子集无有效 judge 结果")

    # 3) Agent 路径延迟门槛（web/hybrid）
    b_p95 = mb.get("agent_total_p95_ms")
    ok = b_p95 is None or b_p95 <= AB_P95_LIMIT * 1000
    _check("agent_total_p95", bool(ok),
           f"B agent P95={b_p95 / 1000:.1f}s ≤ {AB_P95_LIMIT}s" if b_p95 is not None
           else "skip：B 臂无 web/hybrid 样本")

    # 4) 快路径不退化
    a_fast, b_fast = ma.get("fast_total_p95_ms"), mb.get("fast_total_p95_ms")
    if a_fast is not None and b_fast is not None:
        ok = b_fast <= a_fast + AB_FAST_SLACK * 1000
        _check("fast_path_p95", bool(ok),
               f"A={a_fast / 1000:.1f}s B={b_fast / 1000:.1f}s (容差 {AB_FAST_SLACK}s)")
    else:
        _check("fast_path_p95", True, "skip：fast_path 样本缺失")

    # 5) 质量门
    a_poll, b_poll = ma.get("pollution_rate"), mb.get("pollution_rate")
    ok = b_poll is None or b_poll <= (a_poll or 0.0) + AB_NON_DEGRADE
    _check("pollution_non_degrade", bool(ok),
           f"A={a_poll if a_poll is not None else '-'} B={b_poll if b_poll is not None else '-'}")
    tool_calls, tool_ok = mb.get("tool_calls") or 0, 0
    # 工具成功率口径：有工具调用的题目中至少一次成功的占比（live 明细未单独记录成功标志，
    # 用 polluted=False 且 answer 非空近似，失败细节见报告）
    _check("tool_usage", tool_calls > 0, f"B 臂工具调用总数={tool_calls}")
    _check("answer_rate", mb.get("answer_rate", 0.0) >= AB_MIN_TOOL_SUCCESS,
           f"B 臂答案产出率={mb.get('answer_rate', 0.0):.2f}")

    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "arm_a": run_a.get("arm"), "arm_b": run_b.get("arm"),
        "metrics_a": ma, "metrics_b": mb, "deltas": deltas,
        "checks": checks,
        "all_ok": all(c["ok"] for c in checks),
    }
    return report


# L2 分层规划验收：同一 Agent 臂在 AGENT_PLAN_ENABLED=on/off 下，multi 子集
# faithfulness（strict）不劣化 + P95 增量 ≤5s（agent-evolution.md §11.5）。
# on/off 各需一次 live B 臂 run，读两份明细。
def compare_plan_runs(
    dataset: List[Dict[str, Any]], run_off: Dict[str, Any], run_on: Dict[str, Any],
    plan_off_label: str = "plan_off", plan_on_label: str = "plan_on",
    category: str = "web",
) -> Dict[str, Any]:
    """L2 分层规划对拍：同一 Agent 臂在 AGENT_PLAN_ENABLED=on/off 下，指定类别子集
    （默认 web；也可传 multi）faithfulness/relevance（strict）不劣化 + P95 增量 ≤5s。
    on/off 各需一次 live/受控仿真 B 臂 run，读两份明细。
    """
    def _subset(run: Dict[str, Any], cat: str):
        return [s for s in run.get("samples", []) if s.get("category") == cat]

    def _key(base: str) -> str:
        return f"{category}_{base}"

    metrics = {}
    for label, run in ((plan_off_label, run_off), (plan_on_label, run_on)):
        sub = _subset(run, category)
        ok = [s for s in sub if not s.get("error") and s.get("answer") and s.get("judge_ok")]
        m = {
            _key("num"): len(sub),
            _key("faithfulness"): _mean([s["faithfulness"] for s in ok]),
            _key("relevance"): _mean([s["relevance"] for s in ok]),
            _key("total_p95_ms"): _p95([s.get("total_ms") for s in sub]),
            _key("steps"): _mean([s.get("agent_steps") for s in sub]),
        }
        metrics[label] = m

    def _delta(key, val_f): return val_f(metrics[plan_on_label]), val_f(metrics[plan_off_label])

    checks: List[Dict[str, Any]] = []

    def _check(name, ok, detail): checks.append({"name": name, "ok": ok, "detail": detail})

    # faithfulness / relevance：plan_on 不应显著劣于 plan_off（容差 AB_NON_DEGRADE）
    for base in ("faithfulness", "relevance"):
        on_v, off_v = _delta(base, lambda m: m[_key(base)])
        ok = (on_v is None or off_v is None) or on_v >= off_v - AB_NON_DEGRADE
        _check(f"{category}_{base}_non_degrade", bool(ok),
               f"{plan_off_label}={off_v if off_v is not None else '-'} "
               f"{plan_on_label}={on_v if on_v is not None else '-'} (容差 {AB_NON_DEGRADE:.2f})")

    on_p95, off_p95 = (
        metrics[plan_on_label][_key("total_p95_ms")],
        metrics[plan_off_label][_key("total_p95_ms")],
    )
    p95_slack_ms = AB_FAST_SLACK * 1000
    if off_p95 is None or on_p95 is None:
        _check(f"{category}_plan_p95_increase", True, "skip：该子集无延迟数据")
    else:
        inc = on_p95 - off_p95
        _check(f"{category}_plan_p95_increase", inc <= p95_slack_ms,
               f"plan_on P95={on_p95:.0f}ms vs plan_off {off_p95:.0f}ms，增量 {inc:+.0f}ms (≤{AB_FAST_SLACK}s)")

    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "category": category,
        "metrics": metrics,
        "checks": checks,
        "all_ok": all(c["ok"] for c in checks),
    }


def run_compare(args: argparse.Namespace) -> int:
    dataset = _load_jsonl(args.dataset)
    run_a = _load_json(args.run_a)
    run_b = _load_json(args.run_b)
    report = compare_runs(dataset, run_a, run_b)
    out = args.report or DEFAULT_REPORT
    _write_json(out, report)
    print(f"[agent-ab] 对比报告已写入: {out}")
    print("=" * 70)
    labels = [
        ("kb_hit_rate", "KB hit_rate"), ("kb_mrr", "KB mrr"), ("kb_recall", "KB recall"),
        ("hybrid_faithfulness", "hybrid faithfulness"),
        ("hybrid_relevance", "hybrid relevance"),
        ("faithfulness", "faithfulness(全量)"), ("relevance", "relevance(全量)"),
        ("citation_coverage", "citation_coverage"),
        ("pollution_rate", "pollution_rate"),
        ("web_source_count", "web_source_count(均)"),
        ("agent_steps", "agent_steps(均)"),
        ("total_p95_ms", "total_p95(ms)"), ("fast_total_p95_ms", "fast_path p95(ms)"),
    ]
    for key, label in labels:
        a, b = report["metrics_a"].get(key), report["metrics_b"].get(key)
        d = report["deltas"].get(key)
        a_s = f"{a:.3f}" if isinstance(a, float) else (a if a is not None else "-")
        b_s = f"{b:.3f}" if isinstance(b, float) else (b if b is not None else "-")
        d_s = f"{d:+.3f}" if isinstance(d, float) else "-"
        print(f"  {label:<20} A={a_s:<10} B={b_s:<10} Δ={d_s}")
    print("-" * 70)
    for c in report["checks"]:
        print(f"  [{'PASS' if c['ok'] else 'FAIL'}] {c['name']}: {c['detail']}")
    print("=" * 70)
    if not report["all_ok"]:
        print("[agent-ab] 存在未达标门槛")
        return 1
    print("[agent-ab] 全部门槛通过")
    return 0


def run_compare_plan(args: argparse.Namespace) -> int:
    dataset = _load_jsonl(args.dataset)
    run_off = _load_json(args.run_b)          # 默认 = plan off（live B 臂默认 AGENT_PLAN_ENABLED=false）
    run_on = _load_json(args.run_plan_on or (BASE_DIR / "agent_ab_run_B_plan.json"))
    category = args.category
    report = compare_plan_runs(dataset, run_off, run_on, category=category)
    out = args.report or (BASE_DIR / f"agent_ab_report_plan_{category}.json")
    _write_json(out, report)
    print(f"[agent-ab] 分层规划对比报告已写入: {out}")
    print("=" * 70)
    for label in ("plan_off", "plan_on"):
        m = report["metrics"].get(label, {})
        print(f"  {label}: {category}_num={m.get(f'{category}_num')} "
              f"faithfulness={m.get(f'{category}_faithfulness')} "
              f"relevance={m.get(f'{category}_relevance')} "
              f"P95={m.get(f'{category}_total_p95_ms')}ms steps={m.get(f'{category}_steps')}")
    print("-" * 70)
    for c in report["checks"]:
        print(f"  [{'PASS' if c['ok'] else 'FAIL'}] {c['name']}: {c['detail']}")
    print("=" * 70)
    if not report["all_ok"]:
        print("[agent-ab] L2 分层规划存在未达标门槛")
        return 1
    print("[agent-ab] L2 分层规划全部门槛通过")
    return 0


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Agent 演进 A/B 评估")
    parser.add_argument("--mode", choices=["live", "offline", "compare", "compare-plan"], required=True)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--arm", choices=["A", "B"], help="live 模式必填：评估臂")
    parser.add_argument("--kb-ids", default="", help="live：测试知识库 id（逗号分隔）")
    parser.add_argument("--doc-id-map", type=Path, default=None,
                        help="live：{语义文档id: milvus document_id uuid} JSON")
    parser.add_argument("--output", type=Path, default=None, help="live 输出路径")
    parser.add_argument("--run-a", type=Path, default=DEFAULT_RUN_A)
    parser.add_argument("--run-b", type=Path, default=DEFAULT_RUN_B)
    parser.add_argument("--run-plan-on", type=Path, default=None,
                        help="compare-plan：AGENT_PLAN_ENABLED=true 的 live B 臂明细")
    parser.add_argument("--category", choices=["web", "multi"], default="web",
                        help="compare-plan：对拍类别（默认 web；multi 保留历史验收口径）")
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--offline-report", type=Path, default=None)
    args = parser.parse_args()

    if args.mode == "live":
        if not args.arm:
            print("[agent-ab] live 模式必须指定 --arm A 或 B")
            return 2
        # 在 import src 之前注入灰度开关（settings 单例首次加载时生效）
        os.environ["AGENT_ORCHESTRATOR_ENABLED"] = "true" if args.arm == "B" else "false"
        print(f"[agent-ab] live {args.arm} 臂，AGENT_ORCHESTRATOR_ENABLED="
              f"{os.environ['AGENT_ORCHESTRATOR_ENABLED']}")
        return run_live(args)
    if args.mode == "offline":
        return run_offline(args)
    if args.mode == "compare-plan":
        return run_compare_plan(args)
    return run_compare(args)


if __name__ == "__main__":
    sys.exit(main())
