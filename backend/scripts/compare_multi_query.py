"""P2-2 多查询并行检索增益对比（全离线，无需外部服务与 LLM）。

对比两种检索策略在同一评估集（kb_eval_dataset.jsonl + eval_corpus.jsonl）上的表现：
- 基线（single）：单查询 BM25（与 scripts/run_eval.py 生产口径一致）
- 实验（multi）：QueryRewriter（llm=None，仅规则/同义词路径）改写多查询，
  每查询独立 BM25 排序后跨查询 RRF 融合（与生产 search_hybrid_multi 融合口径一致）

局限说明（下界参考）：离线仅有 BM25 单通道、无 LLM 改写、无 rerank 兜底，
LLM 多角度改写与 dense 通道的增益无法离线量化；结论仅用于判断规则改写是否有退化。

用法：
    cd backend && uv run python scripts/compare_multi_query.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))       # 导入同目录 run_eval
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 导入 src 包

from run_eval import (  # noqa: E402
    BASE_DIR,
    DEFAULT_CORPUS,
    DEFAULT_DATASET,
    compute_hit_rate,
    compute_mrr,
    compute_recall,
    load_jsonl,
)

TOP_K = 5
RRF_K = 60


def build_bm25(corpus):
    """构建 BM25 索引（fit 一次，供多查询复用）。"""
    from pymilvus.model.sparse import BM25EmbeddingFunction
    from pymilvus.model.sparse.bm25.tokenizers import build_default_analyzer

    analyzer = build_default_analyzer(language="zh")
    bm25_ef = BM25EmbeddingFunction(analyzer)
    texts = [f"{doc['title']} {doc['text']}" for doc in corpus]
    bm25_ef.fit(texts)
    doc_vecs = bm25_ef.encode_documents(texts)
    return bm25_ef, doc_vecs


def bm25_rank(bm25_ef, doc_vecs, corpus, query, top_k=TOP_K):
    """单查询 BM25 排序，返回按分数降序的文档 id。"""
    query_vec = bm25_ef.encode_queries([query])
    scores = doc_vecs @ query_vec.T
    dense = scores.toarray() if hasattr(scores, "toarray") else scores
    flat = dense.ravel()
    order = flat.argsort()[::-1][:top_k]
    return [corpus[int(i)]["id"] for i in order]


def rrf_fuse(ranked_lists):
    """跨查询 doc 级 RRF 融合（与生产 reciprocal_rank_fusion 口径一致）。"""
    fused = {}
    for ranked in ranked_lists:
        for rank, doc_id in enumerate(ranked, start=1):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (RRF_K + rank)
    return [
        doc_id
        for doc_id, _ in sorted(fused.items(), key=lambda x: x[1], reverse=True)
    ]


async def evaluate(dataset, bm25_ef, doc_vecs, corpus, multi_rewriter=None):
    """跑单查询/多查询两组评估，返回指标。"""
    single = {"hit_rate": [], "mrr": [], "recall": []}
    multi = {"hit_rate": [], "mrr": [], "recall": []}
    multi_query_counts = []

    for sample in dataset:
        golden = sample.get("golden_documents", [])
        question = sample["question"]

        # 基线：单查询
        ranked = bm25_rank(bm25_ef, doc_vecs, corpus, question)
        single["hit_rate"].append(compute_hit_rate(ranked, golden, k=TOP_K))
        single["mrr"].append(compute_mrr(ranked, golden, k=TOP_K))
        single["recall"].append(compute_recall(ranked, golden, k=TOP_K))

        # 实验：多查询 + 跨查询 RRF
        if multi_rewriter is not None:
            queries = await multi_rewriter.rewrite(question) or [question]
        else:
            queries = [question]
        multi_query_counts.append(len(queries))
        ranked_lists = [
            bm25_rank(bm25_ef, doc_vecs, corpus, q) for q in queries
        ]
        fused = rrf_fuse(ranked_lists)
        multi["hit_rate"].append(compute_hit_rate(fused, golden, k=TOP_K))
        multi["mrr"].append(compute_mrr(fused, golden, k=TOP_K))
        multi["recall"].append(compute_recall(fused, golden, k=TOP_K))

    n = len(dataset)
    summarize = lambda m: {  # noqa: E731
        name: sum(vals) / n if n else 0.0 for name, vals in m.items()
    }
    return {
        "single": summarize(single),
        "multi": summarize(multi),
        "avg_multi_query_count": sum(multi_query_counts) / n if n else 0.0,
    }


def main() -> int:
    dataset = load_jsonl(DEFAULT_DATASET)
    corpus = load_jsonl(DEFAULT_CORPUS)
    if not dataset or not corpus:
        print(f"[compare] 数据集或语料为空（dataset={len(dataset)}, corpus={len(corpus)}）")
        return 2

    bm25_ef, doc_vecs = build_bm25(corpus)

    from src.services.query_rewriter import QueryRewriter

    rewriter = QueryRewriter(llm=None)  # 离线禁用 LLM fallback，仅规则/同义词路径

    result = asyncio.run(evaluate(dataset, bm25_ef, doc_vecs, corpus, multi_rewriter=rewriter))

    print("=" * 60)
    print("P2-2 多查询并行检索增益对比（BM25 离线，规则改写，无 rerank）")
    print("=" * 60)
    for name, label in (("single", "单查询基线"), ("multi", "多查询+RRF")):
        m = result[name]
        print(
            f"[{label}] hit_rate@{TOP_K}={m['hit_rate']:.3f}  "
            f"mrr@{TOP_K}={m['mrr']:.3f}  recall@{TOP_K}={m['recall']:.3f}"
        )
    print(f"平均改写查询数: {result['avg_multi_query_count']:.2f}")
    print(f"数据集: {DEFAULT_DATASET.relative_to(BASE_DIR)}  语料: {len(corpus)} 篇")
    print("-" * 60)

    s, m = result["single"], result["multi"]
    degraded = m["hit_rate"] < s["hit_rate"] or m["mrr"] < s["mrr"] or m["recall"] < s["recall"]
    if degraded:
        print("[结论] 多查询（规则改写）存在指标退化，KB_MULTI_QUERY_ENABLED 维持默认关闭")
    else:
        print("[结论] 多查询（规则改写）无退化；LLM 改写与 dense/rerank 增益需真实环境验证")
    return 0


if __name__ == "__main__":
    sys.exit(main())
