"""多查询检索离线 A/B 评估（全离线，无需外部服务 / LLM）。

对比「单查询」与「多查询 + RRF 融合」两臂的检索质量，为打开
KB_MULTI_QUERY_ENABLED 提供量化决策证据（roadmap P2-2：需评估证明增益再开）。

设计说明：
- 数据集 mq_eval_dataset.jsonl 每个样本含 question + query_variants（人工同义改写，
  代替生产侧 LLM QueryRewriter 的改写结果，作为"改写正确时多查询能否提 recall"的上界证据）。
- 语料与指标口径与 run_eval.py 一致（BM25 稀疏栈 / hit_rate@K、mrr@K、recall@K），
  复用 run_eval.load_jsonl 与 compute_*。
- A 臂：仅原 question 单查询，取 Top-K。
- B 臂：question + 各 variant 各自 BM25 Top-channel_top_k，跨查询 RRF 融合后取 Top-K。
  对齐生产多查询口径：KB_RRF_K 平滑因子、KB_MULTI_QUERY_CHANNEL_TOP_K 每路召回。
  （生产在融合后还会用原问题 rerank，离线侧不做重排，只衡量"融合召回"收益上限。）

用法：
    cd backend && uv run python scripts/run_multiquery_ab.py [--report mq_ab.json]

本脚本为决策工具，不做 CI 阈值卡点；仅打印 A/B 对比与是否建议开启多查询的结论。
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = BASE_DIR / "tests" / "evaluation" / "mq_eval_dataset.jsonl"
DEFAULT_CORPUS = BASE_DIR / "tests" / "evaluation" / "eval_corpus.jsonl"

# 与 config.py ProcessingSettings 对齐（KB_RRF_K / KB_MULTI_QUERY_CHANNEL_TOP_K）
RRF_K = 60
CHANNEL_TOP_K = 10


def rrf_fuse(ranked_lists: List[List[str]], k: int = RRF_K) -> List[str]:
    """Reciprocal Rank Fusion：多个等长排名列表按 1/(k+rank) 累加分数，取降序文档列表。

    对齐生产 RRF 融合（RRF_K=60）。返回按融合分降序的文档 id 列表（不去重前的全量）。
    """
    scores: Dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, doc_id in enumerate(ranked, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return [doc_id for doc_id, _ in sorted(scores.items(), key=lambda kv: kv[1], reverse=True)]


def build_bm25(corpus: List[Dict]):
    """构造复用型的 BM25 检索器：对语料 fit 一次，编码全部文档，按查询返回降序文档 id 列表。

    返回 callable bm25(query, top_k) -> List[str]。
    """
    from pymilvus.model.sparse import BM25EmbeddingFunction
    from pymilvus.model.sparse.bm25.tokenizers import build_default_analyzer

    analyzer = build_default_analyzer(language="zh")
    bm25_ef = BM25EmbeddingFunction(analyzer)
    texts = [f"{doc['title']} {doc['text']}" for doc in corpus]
    bm25_ef.fit(texts)
    doc_vecs = bm25_ef.encode_documents(texts)

    def bm25(query: str, top_k: int) -> List[str]:
        query_vec = bm25_ef.encode_queries([query])
        scores = doc_vecs @ query_vec.T
        dense = scores.toarray() if hasattr(scores, "toarray") else scores
        flat = dense.ravel()
        order = flat.argsort()[::-1][:top_k]
        return [corpus[int(i)]["id"] for i in order]

    return bm25


def evaluate_arms(dataset: List[Dict], corpus: List[Dict], top_k: int = 5) -> Dict:
    """跑 A（单查询）/ B（多查询 RRF）两臂，返回逐问题明细与汇总指标。"""
    from run_eval import compute_hit_rate, compute_mrr, compute_recall

    bm25 = build_bm25(corpus)
    details = []
    agg_a = {"hit_rates": [], "mrrs": [], "recalls": []}
    agg_b = {"hit_rates": [], "mrrs": [], "recalls": []}

    for sample in dataset:
        question = sample["question"]
        variants = sample.get("query_variants", [])

        # A 臂：原问题单查询
        a_ranked = bm25(question, top_k)
        a_golden = sample["golden_documents"]

        # B 臂：question + variants 各自召回后 RRF 融合
        ranked_lists = [bm25(question, CHANNEL_TOP_K)]
        ranked_lists += [bm25(v, CHANNEL_TOP_K) for v in variants]
        b_ranked = rrf_fuse(ranked_lists, RRF_K)[:top_k]

        row = {
            "question": question,
            "variants": variants,
            "golden_documents": a_golden,
            "A_ranked_top_k": a_ranked,
            "B_ranked_top_k": b_ranked,
        }
        for arm, ranked in (("A", a_ranked), ("B", b_ranked)):
            row[f"{arm}_hit_rate"] = compute_hit_rate(ranked, a_golden, k=top_k)
            row[f"{arm}_mrr"] = compute_mrr(ranked, a_golden, k=top_k)
            row[f"{arm}_recall"] = compute_recall(ranked, a_golden, k=top_k)
            agg = agg_a if arm == "A" else agg_b
            agg["hit_rates"].append(row[f"{arm}_hit_rate"])
            agg["mrrs"].append(row[f"{arm}_mrr"])
            agg["recalls"].append(row[f"{arm}_recall"])
        details.append(row)

    def summarize(agg: Dict[str, List[float]]) -> Dict[str, float]:
        n = len(agg["hit_rates"]) or 1
        return {
            "hit_rate": sum(agg["hit_rates"]) / n,
            "mrr": sum(agg["mrrs"]) / n,
            "recall": sum(agg["recalls"]) / n,
        }

    return {
        "num_questions": len(dataset),
        "top_k": top_k,
        "channel_top_k": CHANNEL_TOP_K,
        "rrf_k": RRF_K,
        "arm_A_single": summarize(agg_a),
        "arm_B_multiquery": summarize(agg_b),
        "details": details,
    }


def recommend(result: Dict) -> str:
    """基于 A/B 指标给出一条建议（供人决策，非自动开关）。"""
    a = result["arm_A_single"]
    b = result["arm_B_multiquery"]
    recall_gain = b["recall"] - a["recall"]
    hit_gain = b["hit_rate"] - a["hit_rate"]
    # 阈值参考 roadmap：hit_rate > 0.8 / recall 增益阈 0.05
    if hit_gain > 0.05 or (hit_gain > 0.0 and recall_gain > 0.05):
        return (
            f"多查询命中/召回有增益（hit +{hit_gain:.3f} / recall +{recall_gain:.3f}），"
            "可考虑开启 KB_MULTI_QUERY_ENABLED，并用真实 QueryRewriter 在同集上复测确认。"
        )
    return (
        f"多查询未带来明显增益（hit {hit_gain:+.3f} / recall {recall_gain:+.3f}），"
        "结合本地单机延迟放大，建议保持关闭（KB_MULTI_QUERY_ENABLED=false）。"
    )


def main() -> int:
    if str(BASE_DIR / "scripts") not in sys.path:
        sys.path.insert(0, str(BASE_DIR / "scripts"))
    parser = argparse.ArgumentParser(description="多查询离线 A/B 评估（决策工具）")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    from run_eval import load_jsonl

    dataset = load_jsonl(args.dataset)
    corpus = load_jsonl(args.corpus)
    if not dataset or not corpus:
        print(f"[mq-ab] 数据集或语料为空（dataset={len(dataset)}, corpus={len(corpus)}）")
        return 2
    if any("query_variants" not in s or not s.get("query_variants") for s in dataset):
        print("[mq-ab] 存在缺少 query_variants 的样本，无法进行多查询评估")
        return 2

    result = evaluate_arms(dataset, corpus, top_k=args.top_k)
    a, b = result["arm_A_single"], result["arm_B_multiquery"]

    print("=" * 70)
    print("多查询检索 A/B 评估（BM25 离线回归）")
    print("=" * 70)
    print(f"样本数={result['num_questions']}  channel_top_k={result['channel_top_k']}  rrf_k={result['rrf_k']}")
    for d in result["details"]:
        improved = d["B_recall"] > d["A_recall"] + 1e-9
        flag = " ↗多查询提升" if improved else ("=" if abs(d["B_recall"] - d["A_recall"]) <= 1e-9 else " ↘")
        print(f"[{flag}] {d['question']}")
        print(f"        golden={d['golden_documents']}  A={d['A_ranked_top_k']}  B={d['B_ranked_top_k']}")
        print(f"        A: hit={d['A_hit_rate']:.2f} recall={d['A_recall']:.2f} | "
              f"B: hit={d['B_hit_rate']:.2f} recall={d['B_recall']:.2f}")
    print("-" * 70)
    print(f"  A(单查询)      hit@5={a['hit_rate']:.3f}  mrr@5={a['mrr']:.3f}  recall@5={a['recall']:.3f}")
    print(f"  B(多查询+RRF)  hit@5={b['hit_rate']:.3f}  mrr@5={b['mrr']:.3f}  recall@5={b['recall']:.3f}")
    print(f"  结论：{recommend(result)}")

    if args.report:
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[mq-ab] 报告已写入: {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())