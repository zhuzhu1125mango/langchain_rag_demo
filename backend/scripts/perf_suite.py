"""夜间性能回归：把「快不快」变成数字，并且能被卡点。

背景：仓库已有 1659 例功能测试和 `run_eval.py`（检索质量卡点），但**没有任何性能基线**。
`run_eval` 其实每跑一次都会算 `latency_p50/p95_ms`，只是没人比对——push 触发的 CI 只看
质量阈值，延迟数字随 artifact 一起被忽略。于是性能退化只能靠人偶然发现（比如某次
改了融合窗口，单题多花 40ms，谁也不会注意到）。

这个脚本补两件事：

1. **计数器（硬卡点）**：完全确定性的工作量指标——每题打分多少文档、切出多少块、
   融合后多少条。它们与机器快慢无关，所以可以在任何 runner 上逐位比对。
   改算法（加宽重排窗口、多跑一遍融合、切分策略变化）会立刻被这些数字抓住，
   而不需要等延迟变慢。
2. **耗时（按 profile 比，软阈值）**：取多次迭代里的**最小值**作为该项指标。
   最小值不是「最快的一次」，而是「干扰最少的一次」——比均值/中位数更能反映代码本身，
   不会因为 runner 同时跑别的容器而抖成噪音。

为什么不直接卡 `run_eval` 的 p95：那是单题 BM25 打分，只覆盖检索第一步，
且 CI runner 与本地机器差三倍以上，绝对值无法跨机器比较。

用法：
    cd backend && uv run python scripts/perf_suite.py                    # 对比 local 基线
    uv run python scripts/perf_suite.py --profile ci --report perf.json  # CI 用
    uv run python scripts/perf_suite.py --update-baseline               # 重写基线（需人确认）

退出码：0 通过 / 1 退化（计数器不符或耗时超阈值）/ 2 运行错误。

**基线怎么改**：先在目标机器/runner 上跑 `--update-baseline`，看差异是否由「机器变了」
解释；是代码退化就先修代码，**不要**用新基线把退化盖掉。CI 首次运行遇到缺失的 profile
会进入「记录模式」（只打印数字、不卡点），把那次 artifact 里的数字填进基线即可。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "scripts"))

BASELINE_PATH = BASE_DIR.parent / "docs" / "benchmarks" / "perf-baseline.json"

# config 里的模型名没有内置默认（A2），CI/本机缺省时会走不同分支；这里给占位值，
# 让这个脚本在任何环境下都能独立跑起来（不连 Ollama，只做纯计算）。
for _key, _value in (
    ("OLLAMA_MODEL_NAME", "perf_probe"),
    ("FAST_LLM_MODEL_NAME", "perf_probe"),
    ("EMBEDDING_MODEL_NAME", "perf_probe"),
    ("SKIP_OLLAMA_CHECK", "true"),
):
    os.environ.setdefault(_key, _value)


# ---------------------------------------------------------------------------
# 被测负载
# ---------------------------------------------------------------------------

def _synthetic_markdown(target_chars: int = 40000) -> str:
    """造一份确定性 markdown：标题层级 + 表格 + 长段落，覆盖结构化切分的三条分支。

    不用真实文档：真实文档会随业务变化，基线就不稳定了；这里要的是「同一份输入，
    不同版本的代码切出来的块数与耗时」。
    """
    parts: List[str] = ["# 性能基准文档\n"]
    section = 0
    while sum(len(p) for p in parts) < target_chars:
        section += 1
        parts.append(f"\n## 第 {section} 节 制度说明\n")
        parts.append(
            "本段落用于测量切分吞吐，内容重复但不影响结果。"
            * (3 + section % 5)
        )
        parts.append("\n| 项目 | 标准 | 备注 |\n|---|---|---|\n")
        for row in range(6):
            parts.append(f"| 条目 {section}-{row} | 阈值 {row * 10} | 说明文字 {section} |\n")
        parts.append("\n### 第 {0}.1 小节\n".format(section))
        parts.append("子小节正文，用于触发标题路径解析。" * (2 + section % 3))
    return "".join(parts)


def bench_rrf(iterations: int = 300) -> Tuple[Dict[str, float], Dict[str, int]]:
    """RRF 融合：三路 × 50 条（含重叠），测每次融合耗时与融合后的条数。"""
    from src.services.hybrid_search import reciprocal_rank_fusion

    channels: Dict[str, List[Dict]] = {}
    for name, offset in (("dense", 0), ("sparse", 7), ("q2_dense", 13)):
        channels[name] = [
            {"document_id": f"doc-{(i + offset) % 60}", "chunk_index": (i + offset) % 4,
             "content": "x" * 200, "score": 1.0 / (i + 1)}
            for i in range(50)
        ]
    fused = reciprocal_rank_fusion(channels)

    timings = []
    for _ in range(iterations):
        started = time.perf_counter()
        reciprocal_rank_fusion(channels)
        timings.append((time.perf_counter() - started) * 1000)

    counters = {
        "rrf_channel_items": sum(len(v) for v in channels.values()),
        "rrf_fused_items": len(fused),
    }
    return _summarize(timings), counters


def bench_markdown_chunking(iterations: int = 20) -> Tuple[Dict[str, float], Dict[str, int]]:
    """markdown 结构化切分：固定 40KB 输入，测单次耗时与产出块数。"""
    from langchain_core.documents import Document

    from src.services.chunking import ChunkingFactory
    from src.services.document_types import ChunkingStrategy

    text = _synthetic_markdown()
    docs = [Document(page_content=text, metadata={"source": "perf.md"})]

    def split_once():
        return ChunkingFactory.split(
            docs,
            file_path="perf.md",
            chunk_strategy=ChunkingStrategy.MARKDOWN,
            chunk_size=500,
            chunk_overlap=50,
        )

    chunks = split_once()
    timings = []
    for _ in range(iterations):
        started = time.perf_counter()
        split_once()
        timings.append((time.perf_counter() - started) * 1000)

    counters = {"chunk_input_chars": len(text), "chunk_count": len(chunks)}
    return _summarize(timings), counters


def bench_bm25_queries(rounds: int = 5) -> Tuple[Dict[str, float], Dict[str, int]]:
    """BM25 全量查询：评估集 90 题在评估语料上打分（与生产一致的稀疏检索栈）。

    索引构建成本单列，不进单题耗时——它只在启动/重建时发生一次。
    """
    import run_eval  # 同目录脚本：复用它的数据集加载与打分，避免两套实现漂移

    dataset = run_eval.load_jsonl(run_eval.DEFAULT_DATASET)
    corpus = run_eval.load_jsonl(run_eval.DEFAULT_CORPUS)
    bm25_ef, doc_matrix = run_eval.build_index(corpus)

    per_round_total = []
    for _ in range(rounds):
        started = time.perf_counter()
        for sample in dataset:
            run_eval.rank_with_index(bm25_ef, doc_matrix, corpus, sample["question"], top_k=5)
        per_round_total.append((time.perf_counter() - started) * 1000)

    per_query = [t / len(dataset) for t in per_round_total]
    counters = {
        "bm25_questions": len(dataset),
        "bm25_docs_scored_per_query": len(corpus),
    }
    return _summarize(per_query), counters


def _summarize(timings_ms: List[float]) -> Dict[str, float]:
    """取最小值作为「代码本身有多快」，中位数与 p95 一并留档但不参与卡点。

    为什么用最小值：CI runner 是共享的，同一段代码两次跑能差 2~3 倍。
    最小值近似「没有被打断时的耗时」，与机器负载的相关性最低。
    """
    ordered = sorted(timings_ms)
    return {
        "min_ms": round(ordered[0], 3),
        "median_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3),
        "samples": len(ordered),
    }


# ---------------------------------------------------------------------------
# 基线比对
# ---------------------------------------------------------------------------

def compare(current: Dict, baseline: Dict, max_ratio: float, profile: str) -> Tuple[List[str], List[str]]:
    """返回 (failures, notes)。failures 非空即判为退化。

    - 计数器：必须逐项相等（确定性，与机器无关）
    - 耗时：只在**该 profile 已有基线**时比；缺失则只记录
    """
    failures: List[str] = []
    notes: List[str] = []

    base_counters = baseline.get("counters") or {}
    for key, value in current["counters"].items():
        if key not in base_counters:
            notes.append(f"新计数器 {key}={value}（基线里没有，已记录）")
        elif base_counters[key] != value:
            failures.append(f"计数器变化 {key}: 基线 {base_counters[key]} → 现在 {value}")

    base_profile = (baseline.get("profiles") or {}).get(profile)
    if not base_profile:
        notes.append(f"profile '{profile}' 尚无基线：本次只记录，不卡点（把本次数字填进基线后开始卡）")
        return failures, notes

    for item, measured in current["timings"].items():
        base_item = base_profile.get(item)
        if not base_item:
            notes.append(f"新测项 {item}（基线里没有，已记录）")
            continue
        ratio = measured["min_ms"] / base_item["min_ms"] if base_item["min_ms"] else 0.0
        if ratio > max_ratio:
            failures.append(
                f"耗时退化 {item}: {base_item['min_ms']}ms → {measured['min_ms']}ms"
                f"（{ratio:.2f}×，阈值 {max_ratio:.2f}×）"
            )
        else:
            notes.append(f"{item}: {measured['min_ms']}ms / 基线 {base_item['min_ms']}ms = {ratio:.2f}×")
    return failures, notes


def build_report(profile: str) -> Dict:
    timings: Dict[str, Dict[str, float]] = {}
    counters: Dict[str, int] = {}

    for name, fn in (
        ("rrf_fusion", bench_rrf),
        ("markdown_chunking", bench_markdown_chunking),
        ("bm25_query", bench_bm25_queries),
    ):
        item_timing, item_counters = fn()
        timings[name] = item_timing
        counters.update(item_counters)

    return {
        "profile": profile,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "machine": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
        },
        "timings": timings,
        "counters": counters,
    }


def render_markdown(report: Dict, failures: List[str], notes: List[str], baseline_missing: bool) -> str:
    """给 CI 的 $GITHUB_STEP_SUMMARY 用：一张表 + 判定。"""
    lines = [
        f"### 性能回归（profile={report['profile']}）",
        "",
        f"`{report['machine']['platform']}` / {report['machine']['cpu_count']} 核 / "
        f"Python {report['machine']['python']}",
        "",
        "| 测项 | min | median | p95 | 样本 |",
        "|---|---|---|---|---|",
    ]
    for name, t in report["timings"].items():
        lines.append(
            f"| {name} | {t['min_ms']}ms | {t['median_ms']}ms | {t['p95_ms']}ms | {t['samples']} |"
        )
    lines += ["", "| 计数器 | 值 |", "|---|---|"]
    for key, value in report["counters"].items():
        lines.append(f"| {key} | {value} |")

    lines += [""]
    if failures:
        lines.append("**❌ 判定：退化**\n")
        lines += [f"- {f}" for f in failures]
    elif baseline_missing:
        lines.append("**➖ 判定：记录模式**（该 profile 还没有基线，本次不卡点）")
    else:
        lines.append("**✅ 判定：通过**")
    if notes:
        lines += ["", "<details><summary>明细</summary>", ""]
        lines += [f"- {n}" for n in notes]
        lines += ["", "</details>"]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="夜间性能回归（计数器硬卡点 + 耗时软阈值）")
    parser.add_argument("--profile", default="local", help="基线档位：local / ci / 任意名字")
    parser.add_argument("--baseline", type=Path, default=BASELINE_PATH)
    parser.add_argument("--report", type=Path, default=None, help="输出本次结果的 JSON")
    parser.add_argument("--summary", type=Path, default=None, help="输出 markdown 摘要（CI step summary）")
    parser.add_argument("--max-ratio", type=float, default=float(os.environ.get("PERF_MAX_RATIO", "1.6")))
    parser.add_argument("--update-baseline", action="store_true", help="用本次结果写回基线")
    args = parser.parse_args()

    report = build_report(args.profile)

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.update_baseline:
        baseline = {"version": 1, "note": "由 scripts/perf_suite.py --update-baseline 生成", "counters": {}, "profiles": {}}
        if args.baseline.exists():
            baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        baseline.setdefault("counters", {}).update(report["counters"])
        baseline.setdefault("profiles", {})[args.profile] = report["timings"]
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"基线已更新：{args.baseline}（profile={args.profile}）")
        print(render_markdown(report, [], [], False))
        return 0

    baseline: Dict = {}
    if args.baseline.exists():
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    else:
        print(f"[warn] 基线文件不存在：{args.baseline}", file=sys.stderr)

    failures, notes = compare(report, baseline, args.max_ratio, args.profile)
    baseline_missing = not (baseline.get("profiles") or {}).get(args.profile)
    markdown = render_markdown(report, failures, notes, baseline_missing)
    print(markdown)

    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(markdown + "\n", encoding="utf-8")

    if failures:
        print("\n[FAIL] 性能退化：\n  " + "\n  ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
