"""检索通道对比：sparse / dense / hybrid / hybrid+rerank（离线优先，需本机 Ollama）。

回答的问题只有一个：**每条通道到底贡献了多少召回**。
在此之前，项目里说「混合检索更好」只有配置与直觉，没有数字；这个脚本给出同数据集、
同指标口径下的四臂对比，并按题型拆开看（制度问法 / 口语化 / 多跳）。

四臂：
  A sparse        BM25（与生产同一套 jieba 分析器）
  B dense         Ollama bge-m3 余弦相似度
  C hybrid        A、B 各取 channel_top_k 后 RRF 融合（与生产同 k）
  D hybrid+rerank  C 的候选池交给 bge-reranker-base 交叉编码器重排

指标：hit@K / MRR@K / recall@K（文档级，只统计有答案题）；另报无答案题的 top1 分数，
看各通道「能不能区分没有答案」。

用法（本机需先 `ollama serve` 且已 pull bge-m3）：
    cd backend && uv run python scripts/run_channel_ab.py --report ../docs/benchmarks/channel-ab.json
    uv run python scripts/run_channel_ab.py --skip-rerank          # 只比 A/B/C
    uv run python scripts/run_channel_ab.py --arms sparse         # 单臂自检（不需要 Ollama）

注意：本脚本**不进 CI**。它依赖本机 Ollama 与 1GB 级重排模型，跑一次几十秒到几分钟；
CI 里的卡点仍然是 run_eval.py（纯离线 BM25）。这里产出的是「通道增量」这类一次性结论。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_eval import (  # noqa: E402
    DEFAULT_CORPUS,
    DEFAULT_DATASET,
    build_index,
    load_jsonl,
    rank_with_index,
)
from run_multiquery_ab import RRF_K, rrf_fuse  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CACHE = BASE_DIR / "data" / "cache" / "eval_embeddings"
DEFAULT_OLLAMA = os.environ.get("OLLAMA_HOST_URL", "http://127.0.0.1:11434")
DEFAULT_EMBED_MODEL = os.environ.get("EVAL_EMBED_MODEL", "bge-m3:latest")
DEFAULT_RERANK_MODEL = os.environ.get("EVAL_RERANK_MODEL", "BAAI/bge-reranker-base")


# ---------------------------------------------------------------------------
# 指标（与 run_eval 同口径，另加按题型拆分）
# ---------------------------------------------------------------------------

def hit_at(ranked: Sequence[str], golden: Sequence[str], k: int = 5) -> float:
    if not golden:
        return 0.0
    return 1.0 if set(golden) & set(ranked[:k]) else 0.0


def mrr_at(ranked: Sequence[str], golden: Sequence[str], k: int = 5) -> float:
    for rank, doc_id in enumerate(ranked[:k], start=1):
        if doc_id in golden:
            return 1.0 / rank
    return 0.0


def recall_at(ranked: Sequence[str], golden: Sequence[str], k: int = 5) -> float:
    if not golden:
        return 0.0
    return len(set(golden) & set(ranked[:k])) / len(golden)


def score_arm(
    rankings: Dict[str, List[str]],
    dataset: List[Dict],
    top_k: int = 5,
) -> Dict:
    """按题型汇总一个臂的指标。rankings: question -> 排序后的 doc_id 列表。"""
    buckets: Dict[str, List[Dict]] = {}

    for sample in dataset:
        question = sample["question"]
        ranked = rankings.get(question, [])
        golden = sample.get("golden_documents") or []
        category = sample.get("category") or ("multi_hop" if len(golden) > 1 else "policy")
        if not golden:
            continue  # 无答案题不进 hit/mrr；它们的分数口径见 run_eval.py
        buckets.setdefault(category, []).append(
            {
                "hit": hit_at(ranked, golden, top_k),
                "mrr": mrr_at(ranked, golden, top_k),
                "recall": recall_at(ranked, golden, top_k),
            }
        )

    def _mean(rows: List[Dict], key: str) -> float:
        return sum(r[key] for r in rows) / len(rows) if rows else 0.0

    all_rows = [row for rows in buckets.values() for row in rows]
    result = {
        "num_answerable": len(all_rows),
        "hit_rate": round(_mean(all_rows, "hit"), 4),
        "mrr": round(_mean(all_rows, "mrr"), 4),
        "recall": round(_mean(all_rows, "recall"), 4),
        "by_category": {
            cat: {
                "n": len(rows),
                "hit_rate": round(_mean(rows, "hit"), 4),
                "mrr": round(_mean(rows, "mrr"), 4),
            }
            for cat, rows in sorted(buckets.items())
        },
    }
    return result


# ---------------------------------------------------------------------------
# dense：Ollama 嵌入（带磁盘缓存，重跑不重复付费）
# ---------------------------------------------------------------------------

def _cache_path(cache_dir: Path, model: str, texts: List[str]) -> Path:
    digest = hashlib.sha256(("\x00".join([model] + texts)).encode("utf-8")).hexdigest()[:16]
    return cache_dir / f"{digest}.json"


def embed_texts(
    texts: List[str],
    model: str = DEFAULT_EMBED_MODEL,
    base_url: str = DEFAULT_OLLAMA,
    cache_dir: Path = DEFAULT_CACHE,
    batch_size: int = 16,
) -> List[List[float]]:
    """调 Ollama /api/embed 批量取向量，结果按「模型 + 全文」哈希落盘缓存。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = _cache_path(cache_dir, model, texts)
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))

    import urllib.request

    vectors: List[List[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        payload = json.dumps({"model": model, "input": batch}).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/api/embed",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        vectors.extend(data["embeddings"])

    cache_file.write_text(json.dumps(vectors), encoding="utf-8")
    return vectors


def cosine_rank(
    doc_vectors: List[List[float]],
    query_vector: List[float],
    ids: List[str],
    top_k: int = 20,
) -> List[str]:
    def _norm(v: List[float]) -> float:
        return math.sqrt(sum(x * x for x in v)) or 1.0

    qn = _norm(query_vector)
    scored = []
    for doc_id, vec in zip(ids, doc_vectors):
        dot = sum(a * b for a, b in zip(vec, query_vector))
        scored.append((doc_id, dot / (qn * _norm(vec))))
    scored.sort(key=lambda item: item[1], reverse=True)
    return [doc_id for doc_id, _ in scored[:top_k]]


# ---------------------------------------------------------------------------
# 各臂
# ---------------------------------------------------------------------------

def arm_sparse(corpus: List[Dict], dataset: List[Dict], channel_top_k: int) -> Dict[str, List[str]]:
    bm25_ef, doc_matrix = build_index(corpus)
    return {
        s["question"]: [
            doc_id for doc_id, _ in rank_with_index(bm25_ef, doc_matrix, corpus, s["question"], channel_top_k)
        ]
        for s in dataset
    }


def arm_dense(
    corpus: List[Dict],
    dataset: List[Dict],
    channel_top_k: int,
    embed: Callable[[List[str]], List[List[float]]],
) -> Dict[str, List[str]]:
    doc_texts = [f"{doc['title']} {doc['text']}" for doc in corpus]
    ids = [doc["id"] for doc in corpus]
    doc_vectors = embed(doc_texts)
    query_vectors = embed([s["question"] for s in dataset])
    return {
        s["question"]: cosine_rank(doc_vectors, qv, ids, channel_top_k)
        for s, qv in zip(dataset, query_vectors)
    }


def arm_hybrid(
    sparse: Dict[str, List[str]],
    dense: Dict[str, List[str]],
    channel_top_k: int,
) -> Dict[str, List[str]]:
    return {
        question: rrf_fuse([sparse.get(question, []), dense.get(question, [])], k=RRF_K)[:channel_top_k]
        for question in sparse
    }


def arm_rerank(
    hybrid: Dict[str, List[str]],
    dataset: List[Dict],
    corpus_by_id: Dict[str, Dict],
    rerank_fn: Callable[[str, List[str]], List[str]],
) -> Dict[str, List[str]]:
    return {s["question"]: rerank_fn(s["question"], hybrid.get(s["question"], [])) for s in dataset}


def arm_rerank_union(
    sparse: Dict[str, List[str]],
    dense: Dict[str, List[str]],
    dataset: List[Dict],
    corpus_by_id: Dict[str, Dict],
    rerank_fn: Callable[[str, List[str]], List[str]],
    limit: int,
) -> Dict[str, List[str]]:
    """两通道候选**取并集**后直接交给重排（不做 RRF）。

    用来区分两种失败：RRF 把候选压扁/挤掉了，还是候选池本身就没捞到。
    若并集重排的 hit 高于 RRF 重排，说明瓶颈在融合策略而不是重排模型。
    """
    result = {}
    for s in dataset:
        question = s["question"]
        seen, merged = set(), []
        for doc_id in sparse.get(question, []) + dense.get(question, []):
            if doc_id not in seen:
                seen.add(doc_id)
                merged.append(doc_id)
        result[question] = rerank_fn(question, merged[:limit])
    return result


def build_reranker(model_name: str = DEFAULT_RERANK_MODEL, max_length: int = 256):
    """加载本地交叉编码器（离线，CPU）。返回 (query, candidate_texts) -> scores 的函数。"""
    hf_home = os.environ.get("HF_HOME") or str(BASE_DIR / "data" / "model_cache" / "huggingface")
    os.environ.setdefault("HF_HOME", hf_home)
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

    from sentence_transformers import CrossEncoder

    model = CrossEncoder(model_name, max_length=max_length, device="cpu")

    def _predict(query: str, candidates: List[str]) -> List[float]:
        if not candidates:
            return []
        scores = model.predict([(query, text) for text in candidates])
        return [float(x) for x in scores]

    return _predict


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="检索通道 A/B（sparse / dense / hybrid / hybrid+rerank）")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--top-k", type=int, default=5, help="最终截断的 K（与 CI 口径一致）")
    parser.add_argument("--channel-top-k", type=int, default=20, help="融合前各通道保留的候选数")
    parser.add_argument("--arms", default="sparse,dense,hybrid,rerank", help="要跑的臂：sparse,dense,hybrid,rerank,union,dense_rerank")
    parser.add_argument("--skip-rerank", action="store_true")
    parser.add_argument("--ollama", default=DEFAULT_OLLAMA)
    parser.add_argument("--embed-model", default=DEFAULT_EMBED_MODEL)
    parser.add_argument("--rerank-model", default=DEFAULT_RERANK_MODEL)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    arms = {a.strip() for a in args.arms.split(",") if a.strip()}
    if args.skip_rerank:
        arms.discard("rerank")

    dataset = load_jsonl(args.dataset)
    corpus = load_jsonl(args.corpus)
    corpus_by_id = {doc["id"]: doc for doc in corpus}
    print(f"[ab] 题目 {len(dataset)} / 语料 {len(corpus)} / K={args.top_k} / 通道候选 {args.channel_top_k}")

    results: Dict[str, Dict] = {}
    rankings: Dict[str, Dict[str, List[str]]] = {}
    timings: Dict[str, float] = {}

    def _run(name: str, fn: Callable[[], Dict[str, List[str]]]) -> None:
        started = time.perf_counter()
        rankings[name] = fn()
        timings[name] = round(time.perf_counter() - started, 2)
        # 无答案题的 top1 分数无法从 doc_id 列表反推，这里只统计有答案题
        results[name] = score_arm(rankings[name], dataset, args.top_k)
        print(f"[ab] {name:<7} done {timings[name]:>6.2f}s")

    if "sparse" in arms:
        _run("sparse", lambda: arm_sparse(corpus, dataset, args.channel_top_k))

    need_dense = arms & {"dense", "hybrid", "rerank", "union", "dense_rerank"}
    if need_dense:
        def _embed(texts: List[str]) -> List[List[float]]:
            return embed_texts(texts, model=args.embed_model, base_url=args.ollama)

        try:
            _embed(["ping"])
        except Exception as exc:  # noqa: BLE001 - 这里就要把所有连接问题拦成一句话
            print(f"[ab] Ollama 不可用（{args.ollama}）：{exc}")
            print("[ab] dense/hybrid/rerank 无法评估；先 `ollama serve` 并 pull 嵌入模型，或只用 --arms sparse")
            return 2

        if "dense" in arms:
            _run("dense", lambda: arm_dense(corpus, dataset, args.channel_top_k, _embed))
        if arms & {"hybrid", "rerank", "union", "dense_rerank"}:
            if "sparse" not in rankings:
                _run("sparse", lambda: arm_sparse(corpus, dataset, args.channel_top_k))
            if "dense" not in rankings:
                _run("dense", lambda: arm_dense(corpus, dataset, args.channel_top_k, _embed))
            _run("hybrid", lambda: arm_hybrid(rankings["sparse"], rankings["dense"], args.channel_top_k))

        if arms & {"rerank", "union", "dense_rerank"}:
            predict = build_reranker(args.rerank_model)

            def _rerank(question: str, candidates: List[str]) -> List[str]:
                texts = [f"{corpus_by_id[c]['title']} {corpus_by_id[c]['text']}" for c in candidates]
                scores = predict(question, texts)
                order = sorted(range(len(candidates)), key=lambda i: scores[i], reverse=True)
                return [candidates[i] for i in order][: args.top_k]

            if "rerank" in arms:
                _run("rerank", lambda: arm_rerank(rankings["hybrid"], dataset, corpus_by_id, _rerank))
            if "dense_rerank" in arms:
                _run("dense_rerank", lambda: arm_rerank(rankings["dense"], dataset, corpus_by_id, _rerank))
            if "union" in arms:
                _run(
                    "union",
                    lambda: arm_rerank_union(
                        rankings["sparse"],
                        rankings["dense"],
                        dataset,
                        corpus_by_id,
                        _rerank,
                        limit=args.channel_top_k * 2,
                    ),
                )

    # ---- 汇总 ----
    print("=" * 78)
    print(f"{'arm':<8}{'hit@' + str(args.top_k):>10}{'mrr':>8}{'recall':>8}   "
          f"{'policy':>8}{'colloquial':>12}{'multi_hop':>11}")
    for name in ("sparse", "dense", "hybrid", "rerank", "union", "dense_rerank"):
        r = results.get(name)
        if not r:
            continue
        cat = r["by_category"]
        print(
            f"{name:<8}{r['hit_rate']:>10.3f}{r['mrr']:>8.3f}{r['recall']:>8.3f}   "
            f"{cat.get('policy', {}).get('hit_rate', float('nan')):>8.3f}"
            f"{cat.get('colloquial', {}).get('hit_rate', float('nan')):>12.3f}"
            f"{cat.get('multi_hop', {}).get('hit_rate', float('nan')):>11.3f}"
        )
    print("-" * 78)
    print("注：dense/rerank 的耗时包含模型加载与网络，不代表单次查询延迟。")

    if args.report:
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "config": {
                "top_k": args.top_k,
                "channel_top_k": args.channel_top_k,
                "rrf_k": RRF_K,
                "embed_model": args.embed_model,
                "rerank_model": args.rerank_model if "rerank" in results else None,
                "corpus_docs": len(corpus),
                "questions": len(dataset),
            },
            "arms": results,
            "timings_seconds": timings,
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[ab] 报告已写入: {args.report}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
