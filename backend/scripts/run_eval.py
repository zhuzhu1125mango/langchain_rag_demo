"""RAG 检索质量评估脚本（全离线，无需外部服务）。

基于 tests/evaluation/kb_eval_dataset.jsonl（问题 + golden 文档）与
eval_corpus.jsonl（评估语料），使用与生产一致的 BM25 稀疏检索栈
（pymilvus.model.sparse）对检索链路做回归评估。

指标说明（文档级，Top-K=5）：
- hit_rate@K：命中任一 golden 文档的问题比例（context precision 的回归代理）
- mrr@K：首个命中位置的倒数排名均值
- recall@K：平均命中的 golden 文档比例（context recall 的回归代理）
- no_answer_separation：**无答案题的 top1 分数 ÷ 有答案题的 top1 分数中位数**。
  它回答的是「能不能只靠检索分数判断『语料里没有』」——接近 1 说明分数通道
  分不开，拒答必须交给生成层（LLM）判断，不能设个阈值就假装能拒。
- latency_p50/p95_ms：单题排序耗时（本地 BM25 打分，**不含**向量库/网络/LLM），
  是延迟的地板值，不是端到端 P95。

有答案题与无答案题分开统计：把无答案题算进 hit_rate 会让指标变成
「拒答率」与「召回率」的混合体，两种失败无法区分。

用法：
    cd backend && uv run python scripts/run_eval.py --report eval_report.json

阈值卡点（环境变量可覆盖）：
    EVAL_MIN_HIT_RATE（默认 0.85，2026-11-02 基线 0.904，留约 5pp 回退空间）
    EVAL_MIN_MRR（默认 0.80，基线 0.854）
    EVAL_MIN_RECALL（默认 0.85，基线 0.904）
低于任一阈值时进程以非零码退出，供 CI rag-eval job 卡点。
阈值不是「行业标准」而是**本数据集上的基线减回退空间**：要调整就先改数据集、
重跑、把新数字写进 docs/benchmarks/，再动阈值——顺序反了就成了「改阈值让指标好看」。
无答案题的分离度只报告、不卡点：它反映语料与问题的性质，不是代码质量。
"""

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = BASE_DIR / "tests" / "evaluation" / "kb_eval_dataset.jsonl"
DEFAULT_CORPUS = BASE_DIR / "tests" / "evaluation" / "eval_corpus.jsonl"


def get_thresholds() -> Dict[str, float]:
    """读取阈值（环境变量可覆盖默认值），调用时读取以便测试与 CI 覆盖。"""
    return {
        "hit_rate": float(os.environ.get("EVAL_MIN_HIT_RATE", "0.85")),
        "mrr": float(os.environ.get("EVAL_MIN_MRR", "0.80")),
        "recall": float(os.environ.get("EVAL_MIN_RECALL", "0.85")),
    }


def load_jsonl(path: Path) -> List[Dict]:
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def compute_hit_rate(results: List[str], golden_documents: List[str], k: int = 5) -> float:
    """Top-K 中是否命中任一 golden document。"""
    if not golden_documents:
        return 0.0
    return 1.0 if set(golden_documents) & set(results[:k]) else 0.0


def compute_mrr(results: List[str], golden_documents: List[str], k: int = 5) -> float:
    """第一个命中 golden document 的结果的倒数排名。"""
    for rank, doc_id in enumerate(results[:k], start=1):
        if doc_id in golden_documents:
            return 1.0 / rank
    return 0.0


def compute_recall(results: List[str], golden_documents: List[str], k: int = 5) -> float:
    """命中 golden document 的比例。"""
    if not golden_documents:
        return 0.0
    hits = len(set(golden_documents) & set(results[:k]))
    return hits / len(golden_documents)


# ---------------------------------------------------------------------------
# 检索索引
# ---------------------------------------------------------------------------

def build_index(corpus: List[Dict]):
    """在整份语料上 fit 一次 BM25，返回 (embedding_fn, 文档向量矩阵)。

    原来每题都重新 fit：40 题 × 60 篇会把「拟合语料」的成本重复计入每次查询，
    既慢又把「语料统计」和「查询打分」两段耗时混在一起，测不出真实的查询延迟。
    """
    from pymilvus.model.sparse import BM25EmbeddingFunction
    from pymilvus.model.sparse.bm25.tokenizers import build_default_analyzer

    analyzer = build_default_analyzer(language="zh")
    bm25_ef = BM25EmbeddingFunction(analyzer)
    texts = [f"{doc['title']} {doc['text']}" for doc in corpus]
    bm25_ef.fit(texts)
    return bm25_ef, bm25_ef.encode_documents(texts)


def rank_with_index(bm25_ef, doc_matrix, corpus: List[Dict], query: str, top_k: int = 5) -> List[Tuple[str, float]]:
    """按 BM25 分数降序返回 [(doc_id, score), ...]。"""
    query_vec = bm25_ef.encode_queries([query])
    scores = doc_matrix @ query_vec.T
    dense = scores.toarray() if hasattr(scores, "toarray") else scores
    flat = dense.ravel()
    order = flat.argsort()[::-1][:top_k]
    return [(corpus[int(i)]["id"], float(flat[int(i)])) for i in order]


def bm25_rank(corpus: List[Dict], query: str, top_k: int = 5) -> List[str]:
    """单次查询的便捷封装（每次重建索引，仅用于脚本/调试）。"""
    bm25_ef, doc_matrix = build_index(corpus)
    return [doc_id for doc_id, _ in rank_with_index(bm25_ef, doc_matrix, corpus, query, top_k)]


def evaluate(dataset: List[Dict], corpus: List[Dict], top_k: int = 5) -> Dict:
    """跑完整评估，返回逐问题明细与汇总指标。

    有答案题（golden_documents 非空）算 hit/mrr/recall；
    无答案题（expect_no_answer 或 golden 为空）只算 top1 分数的分布，
    用来判断「分数通道能不能区分有无答案」。
    """
    bm25_ef, doc_matrix = build_index(corpus)
    # 索引构建成本单列，不摊进单题延迟
    details = []
    hit_rates, mrrs, recalls = [], [], []
    latency_ms: List[float] = []
    answerable_top1: List[float] = []
    no_answer_top1: List[float] = []

    for sample in dataset:
        started = time.perf_counter()
        ranked = rank_with_index(bm25_ef, doc_matrix, corpus, sample["question"], top_k=top_k)
        elapsed = (time.perf_counter() - started) * 1000
        latency_ms.append(elapsed)

        golden = sample.get("golden_documents", [])
        ranked_ids = [doc_id for doc_id, _ in ranked]
        top1_score = ranked[0][1] if ranked else 0.0

        # 先置 None：无答案题不进这三项，否则会复用上一题的残值
        hit = mrr = recall = None
        if golden:
            hit = compute_hit_rate(ranked_ids, golden, k=top_k)
            mrr = compute_mrr(ranked_ids, golden, k=top_k)
            recall = compute_recall(ranked_ids, golden, k=top_k)
            hit_rates.append(hit)
            mrrs.append(mrr)
            recalls.append(recall)
            answerable_top1.append(top1_score)
        else:
            no_answer_top1.append(top1_score)

        details.append(
            {
                "question": sample["question"],
                "golden_documents": golden,
                "expect_no_answer": not golden,
                "ranked_top_k": ranked_ids,
                "top1_score": round(top1_score, 4),
                "hit_rate": hit,
                "mrr": mrr,
                "recall": recall,
                "latency_ms": round(elapsed, 3),
            }
        )

    n_answerable = len(hit_rates)
    metrics = {
        "num_questions": len(dataset),
        "num_answerable": n_answerable,
        "num_no_answer": len(no_answer_top1),
        "num_corpus_docs": len(corpus),
        "top_k": top_k,
        "hit_rate": sum(hit_rates) / n_answerable if n_answerable else 0.0,
        "mrr": sum(mrrs) / n_answerable if n_answerable else 0.0,
        "recall": sum(recalls) / n_answerable if n_answerable else 0.0,
        "latency_p50_ms": round(statistics.median(latency_ms), 3) if latency_ms else 0.0,
        "latency_p95_ms": round(_percentile(latency_ms, 95), 3) if latency_ms else 0.0,
    }

    if answerable_top1 and no_answer_top1:
        answerable_median = statistics.median(answerable_top1)
        no_answer_median = statistics.median(no_answer_top1)
        metrics["answerable_top1_median"] = round(answerable_median, 4)
        metrics["no_answer_top1_median"] = round(no_answer_median, 4)
        metrics["no_answer_separation"] = (
            round(no_answer_median / answerable_median, 4) if answerable_median > 0 else None
        )

    return {"metrics": metrics, "details": details}


def _percentile(values: List[float], pct: float) -> float:
    """线性插值百分位（样本量小，nearest-rank 会失真）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * pct / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def main() -> int:
    parser = argparse.ArgumentParser(description="RAG 检索质量离线评估")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--report", type=Path, default=None, help="输出 JSON 指标报告路径")
    parser.add_argument(
        "--show-misses",
        action="store_true",
        help="只打印未命中的题目（排查用）",
    )
    args = parser.parse_args()

    dataset = load_jsonl(args.dataset)
    corpus = load_jsonl(args.corpus)
    if not dataset or not corpus:
        print(f"[eval] 数据集或语料为空（dataset={len(dataset)}, corpus={len(corpus)}）")
        return 2

    result = evaluate(dataset, corpus, top_k=args.top_k)
    metrics = result["metrics"]

    print("=" * 60)
    print("RAG 检索质量评估报告（BM25 离线回归）")
    print("=" * 60)
    for d in result["details"]:
        if d["expect_no_answer"]:
            status = "NOANS"
        elif d["hit_rate"]:
            status = "HIT  "
        else:
            status = "MISS "
        if args.show_misses and status == "HIT  ":
            continue
        print(f"[{status}] {d['question']}")
        print(f"         golden={d['golden_documents']} top{args.top_k}={d['ranked_top_k']} top1={d['top1_score']}")
    print("-" * 60)
    print(
        f"hit_rate@{args.top_k}={metrics['hit_rate']:.3f}  "
        f"mrr@{args.top_k}={metrics['mrr']:.3f}  "
        f"recall@{args.top_k}={metrics['recall']:.3f}"
    )
    print(
        f"(有答案 {metrics['num_answerable']} 题 / 无答案 {metrics['num_no_answer']} 题 / "
        f"语料 {metrics['num_corpus_docs']} 篇 / 单题排序 p50={metrics['latency_p50_ms']}ms "
        f"p95={metrics['latency_p95_ms']}ms)"
    )
    if "no_answer_separation" in metrics:
        print(
            f"无答案分离度={metrics['no_answer_separation']} "
            f"（top1 中位数：有答案 {metrics['answerable_top1_median']} / "
            f"无答案 {metrics['no_answer_top1_median']}；接近 1 表示分数分不开，拒答只能交给生成层）"
        )

    if args.report:
        args.report.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[eval] 指标报告已写入: {args.report}")

    failed = [
        (name, metrics[name], thr)
        for name, thr in get_thresholds().items()
        if metrics[name] < thr
    ]
    if failed:
        for name, actual, thr in failed:
            print(f"[eval] 未达阈值: {name}={actual:.3f} < {thr}")
        return 1
    print(f"[eval] 全部指标达标（阈值: {get_thresholds()}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
