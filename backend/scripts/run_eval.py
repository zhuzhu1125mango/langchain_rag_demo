"""RAG 检索质量评估脚本（全离线，无需外部服务）。

基于 tests/evaluation/kb_eval_dataset.jsonl（问题 + golden 文档）与
eval_corpus.jsonl（评估语料），使用与生产一致的 BM25 稀疏检索栈
（pymilvus.model.sparse）对检索链路做回归评估。

指标说明（文档级，Top-K=5）：
- hit_rate@5：命中任一 golden 文档的问题比例（对应 context precision 的回归代理）
- mrr@5：首个命中位置的倒数排名均值
- recall@5：平均命中的 golden 文档比例（对应 context recall 的回归代理）

用法：
    cd backend && uv run python scripts/run_eval.py --report eval_report.json

阈值卡点（环境变量可覆盖）：
    EVAL_MIN_HIT_RATE（默认 0.8，行业标准参考）
    EVAL_MIN_MRR（默认 0.5，按基线设定）
    EVAL_MIN_RECALL（默认 0.5，按基线设定）
低于任一阈值时进程以非零码退出，供 CI rag-eval job 卡点。
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict, List

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = BASE_DIR / "tests" / "evaluation" / "kb_eval_dataset.jsonl"
DEFAULT_CORPUS = BASE_DIR / "tests" / "evaluation" / "eval_corpus.jsonl"


def get_thresholds() -> Dict[str, float]:
    """读取阈值（环境变量可覆盖默认值），调用时读取以便测试与 CI 覆盖。"""
    return {
        "hit_rate": float(os.environ.get("EVAL_MIN_HIT_RATE", "0.8")),
        "mrr": float(os.environ.get("EVAL_MIN_MRR", "0.5")),
        "recall": float(os.environ.get("EVAL_MIN_RECALL", "0.5")),
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


def bm25_rank(corpus: List[Dict], query: str, top_k: int = 5) -> List[str]:
    """用与生产一致的 BM25 稀疏检索栈对语料排序，返回按分数降序的文档 id。"""
    from pymilvus.model.sparse import BM25EmbeddingFunction
    from pymilvus.model.sparse.bm25.tokenizers import build_default_analyzer

    analyzer = build_default_analyzer(language="zh")
    bm25_ef = BM25EmbeddingFunction(analyzer)
    texts = [f"{doc['title']} {doc['text']}" for doc in corpus]
    bm25_ef.fit(texts)
    doc_vecs = bm25_ef.encode_documents(texts)
    query_vec = bm25_ef.encode_queries([query])

    scores = doc_vecs @ query_vec.T
    dense = scores.toarray() if hasattr(scores, "toarray") else scores
    flat = dense.ravel()
    order = flat.argsort()[::-1][:top_k]
    return [corpus[int(i)]["id"] for i in order]


def evaluate(dataset: List[Dict], corpus: List[Dict], top_k: int = 5) -> Dict:
    """跑完整评估，返回逐问题明细与汇总指标。"""
    details = []
    hit_rates, mrrs, recalls = [], [], []
    for sample in dataset:
        ranked = bm25_rank(corpus, sample["question"], top_k=top_k)
        golden = sample.get("golden_documents", [])
        hit = compute_hit_rate(ranked, golden, k=top_k)
        mrr = compute_mrr(ranked, golden, k=top_k)
        recall = compute_recall(ranked, golden, k=top_k)
        hit_rates.append(hit)
        mrrs.append(mrr)
        recalls.append(recall)
        details.append(
            {
                "question": sample["question"],
                "golden_documents": golden,
                "ranked_top_k": ranked,
                "hit_rate": hit,
                "mrr": mrr,
                "recall": recall,
            }
        )

    n = len(dataset)
    metrics = {
        "num_questions": n,
        "num_corpus_docs": len(corpus),
        "top_k": top_k,
        "hit_rate": sum(hit_rates) / n if n else 0.0,
        "mrr": sum(mrrs) / n if n else 0.0,
        "recall": sum(recalls) / n if n else 0.0,
    }
    return {"metrics": metrics, "details": details}


def main() -> int:
    parser = argparse.ArgumentParser(description="RAG 检索质量离线评估")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--report", type=Path, default=None, help="输出 JSON 指标报告路径")
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
        status = "HIT " if d["hit_rate"] > 0 else "MISS"
        print(f"[{status}] {d['question']}")
        print(f"       golden={d['golden_documents']} top{args.top_k}={d['ranked_top_k']}")
    print("-" * 60)
    print(
        f"hit_rate@{args.top_k}={metrics['hit_rate']:.3f}  "
        f"mrr@{args.top_k}={metrics['mrr']:.3f}  "
        f"recall@{args.top_k}={metrics['recall']:.3f}  "
        f"(questions={metrics['num_questions']}, corpus={metrics['num_corpus_docs']})"
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
