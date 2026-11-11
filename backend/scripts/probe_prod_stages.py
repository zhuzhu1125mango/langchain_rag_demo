"""在生产索引上重跑 24 题逐级探针（线上口径）。

为什么需要第二个探针
--------------------
`probe_retrieval_stages.py` 在**仓库文档上自建索引**：好处是能随意改切片、改融合、
改选择规则；代价是它测的从来不是「线上那套索引 + 那套参数」。2026-11-10 重灌之后，
生产语料与探针语料终于同源（同样 4 篇文档、同一切分器、同样 606 块），
本地结论第一次可以直接对到线上 —— 那就该对一次，而不是假设它能迁移。

本脚本与本地探针的区别只有一处：**索引与重排模型都是生产在用的那一套**。
它复刻生产 `search_hybrid` 的真实顺序（两路各 20 → RRF → 前 N 条送重排 → 取 k 条），
只把 N 当变量扫描，从而把「候选池 / 重排窗口 / 最终块数」三级损失分开。

只读
----
不写 Milvus、不改配置、不重启、不触发摄入。跑一次的成本是
「24 题 × (2 路召回 + 4 次重排)」，CPU 上约两三分钟。

生产口径（2026-11-10 实测）
--------------------------
- `KB_HYBRID_SEARCH_TOP_K` 未设 → 默认 20，两路各取 20；
- `KB_HYBRID_RERANK_TOP_K=3`，且代码是 `rerank_n = max(k, 该值)` ——
  **重排窗口只有 3**，重排只能在 RRF 的前 3 名里排序，无法补充候选；
- 最终 `k=3` 块进上下文。

用法（生产容器内）
------------------
    python scripts/probe_prod_stages.py                       # 默认扫 3,5,10,20
    python scripts/probe_prod_stages.py --report /app/data/probe-prod.json
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)
sys.path.insert(0, os.path.join(BACKEND_DIR, "scripts"))

from src.config import settings  # noqa: E402
from src.services.hybrid_search import (  # noqa: E402
    reciprocal_rank_fusion,
    rerank_results,
)
from src.services.milvus_common import _ab_rerank_top_k  # noqa: E402
from src.services.milvus_service import MilvusService  # noqa: E402
from src.services.table_chunks import fold_same_table_rows  # noqa: E402

# 复用同一批题与同一个覆盖率定义——两套口径算出来的数不可比，那就白测了
from probe_retrieval_stages import QUESTIONS, coverage  # noqa: E402


def best_fact_rank(
    ranked: Sequence[str], corpus_by_id: Dict[str, Dict], facts: Sequence[str]
) -> Tuple[Optional[int], int]:
    """第一个含事实的片段的名次（1 起），以及它命中几个事实。

    名次即是「候选池够不够」的答案：名次大于窗口宽度的答案，结构上不可能被选中。
    """
    for rank, doc_id in enumerate(ranked, 1):
        text = corpus_by_id.get(doc_id, {}).get("content") or ""
        hits = [f for f in facts if f in text]
        if hits:
            return rank, len(hits)
    return None, 0


async def fetch_corpus(svc) -> Dict[str, Dict]:
    """把整个集合读进内存（606 行），用于找答案块与邻块回填。"""
    rows = await svc.client.query(
        collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
        filter="id != ''",
        output_fields=["id", "document_id", "chunk_index", "content", "heading_path"],
        limit=16384,
    )
    return {r["id"]: r for r in rows}


def neighbor_ids(picked: Sequence[str], corpus_by_id: Dict[str, Dict]) -> List[str]:
    """选中片段的同文档相邻块（index±1），保持「先原地、再邻块」的顺序。

    概览型问题常把一条事实拆在相邻两块里，重排只给其中一块打分；
    回填邻块是唯一不依赖重排能力的补全手段。
    """
    index: Dict[Tuple[str, int], str] = {}
    for doc_id, row in corpus_by_id.items():
        index[(row.get("document_id"), row.get("chunk_index"))] = doc_id

    out: List[str] = list(picked)
    seen = set(out)
    for doc_id in picked:
        row = corpus_by_id.get(doc_id) or {}
        doc, idx = row.get("document_id"), row.get("chunk_index")
        if doc is None or idx is None:
            continue
        for delta in (-1, 1):
            nb = index.get((doc, idx + delta))
            if nb and nb not in seen:
                out.append(nb)
                seen.add(nb)
    return out


async def probe_one(svc, item: Dict, corpus_by_id: Dict[str, Dict], args) -> Dict[str, Any]:
    q, facts = item["q"], item["facts"]
    pool = args.pool

    dense = await svc.search_dense(q, k=pool)
    sparse = await svc.search_sparse(q, k=pool)
    fused = reciprocal_rank_fusion({"dense": dense, "sparse": sparse}, k=settings.processing.KB_RRF_K)

    # 可选：在生产候选上折叠同表行级块（与 search_hybrid 里 KB_TABLE_FOLD_ENABLED 同一函数），
    # 用于在不改线上配置的前提下验证这条改动在真实索引上的效果。
    fused_before_fold = len(fused)
    fold_removed = 0
    if getattr(args, "fold_table", False):
        fused = fold_same_table_rows(fused)
        fold_removed = fused_before_fold - len(fused)

    fused_ids = [r["id"] for r in fused]
    fused_rank, fused_hits = best_fact_rank(fused_ids, corpus_by_id, facts)

    def _cov(ids: Sequence[str]) -> Tuple[float, List[str]]:
        return coverage([corpus_by_id[i]["content"] for i in ids if i in corpus_by_id], facts)

    row: Dict[str, Any] = {
        "question": q,
        "kind": item["kind"],
        "facts": len(facts),
        "dense_returned": len(dense),
        "sparse_returned": len(sparse),
        "fused_returned": len(fused),
        "fused_before_fold": fused_before_fold,
        "fold_removed": fold_removed,
        "fused_rank": fused_rank,
        "fused_hits_at_rank": fused_hits,
        "channel_top_k": {
            # 单通道单独看：这一路自己能不能把答案带进前 k
            "dense": round(_cov([r["id"] for r in dense[: args.final_k]])[0], 3),
            "sparse": round(_cov([r["id"] for r in sparse[: args.final_k]])[0], 3),
            "dense_pool": round(_cov([r["id"] for r in dense])[0], 3),
            "sparse_pool": round(_cov([r["id"] for r in sparse])[0], 3),
        },
        "pool_coverage": round(_cov(fused_ids)[0], 3),
        "stages": {},
    }

    for n in args.rerank_n:
        candidates = fused[:n]
        reranked = await rerank_results(q, candidates, top_k=args.final_k)
        picked = [r["id"] for r in reranked]
        frac, hits = _cov(picked)

        answer_rank = None
        for rank, r in enumerate(reranked, 1):
            if any(f in (r.get("content") or "") for f in facts):
                answer_rank = rank
                break

        expanded = neighbor_ids(picked, corpus_by_id)
        exp_frac, _ = _cov(expanded)

        row["stages"][f"rerank_n{n}"] = {
            "coverage": round(frac, 3),
            "hits": hits,
            "picked": [
                {
                    "id": r["id"],
                    "rerank_score": round(float(r.get("rerank_score") or 0), 4),
                    "heading_path": r.get("heading_path") or "",
                    "preview": (r.get("content") or "")[:80].replace("\n", " "),
                }
                for r in reranked
            ],
            "answer_rerank_rank": answer_rank,
            "to_expand_coverage": round(exp_frac, 3),
        }
    return row


async def main_async(args) -> int:
    svc = await MilvusService.get_instance()
    corpus_by_id = await fetch_corpus(svc)
    print(f"集合 {settings.milvus.MILVUS_COLLECTION_NAME}：{len(corpus_by_id)} 块")
    print(
        f"生产口径：通道 {args.pool} / 重排窗口 {max(args.final_k, _ab_rerank_top_k())}"
        f" / 最终 {args.final_k} 块 / rerank={'on' if settings.processing.KB_RERANK_ENABLED else 'off'}"
    )

    rows: List[Dict[str, Any]] = []
    for item in QUESTIONS:
        row = await probe_one(svc, item, corpus_by_id, args)
        rows.append(row)
        zeros = [n for n in args.rerank_n if row["stages"][f"rerank_n{n}"]["coverage"] == 0]
        flag = f" ⚠零覆盖(n={','.join(map(str, zeros))})" if zeros else ""
        print(
            f"  [{row['kind'][:4]}] fused_rank={row['fused_rank']} "
            f"n{args.rerank_n[0]}={row['stages'][f'rerank_n{args.rerank_n[0]}']['coverage']:.2f}{flag} "
            f"| {row['question'][:26]}"
        )

    summary: Dict[str, Any] = {}
    for kind in ("overview", "detail"):
        subset = [r for r in rows if r["kind"] == kind]
        if not subset:
            continue
        summary[kind] = {
            "n": len(subset),
            "pool_coverage": round(statistics.mean(r["pool_coverage"] for r in subset), 3),
            "fused_rank_le_20": sum(1 for r in subset if r["fused_rank"] and r["fused_rank"] <= 20),
            "fused_rank_le_10": sum(1 for r in subset if r["fused_rank"] and r["fused_rank"] <= 10),
            "coverage": {
                f"n{n}": round(statistics.mean(r["stages"][f"rerank_n{n}"]["coverage"] for r in subset), 3)
                for n in args.rerank_n
            },
            "coverage_with_expand": {
                f"n{n}": round(
                    statistics.mean(r["stages"][f"rerank_n{n}"]["to_expand_coverage"] for r in subset), 3
                )
                for n in args.rerank_n
            },
            "dense_only_topk": round(statistics.mean(r["channel_top_k"]["dense"] for r in subset), 3),
            "sparse_only_topk": round(statistics.mean(r["channel_top_k"]["sparse"] for r in subset), 3),
        }

    print("\n按题型汇总")
    for kind, s in summary.items():
        print(f"  {kind:9} n={s['n']}  候选池 {s['pool_coverage']}  "
              f"覆盖 {s['coverage']}  邻块回填后 {s['coverage_with_expand']}")
        print(f"            单通道 top{args.final_k}: dense {s['dense_only_topk']} / sparse {s['sparse_only_topk']}"
              f"  答案在融合 top10 内 {s['fused_rank_le_10']}/{s['n']}")

    if args.report:
        payload = {
            "mode": "prod",
            "at": datetime.now().isoformat(timespec="seconds"),
            "collection": settings.milvus.MILVUS_COLLECTION_NAME,
            "chunks": len(corpus_by_id),
            "config": {
                "channel_pool": args.pool,
                "rerank_top_k": settings.processing.KB_HYBRID_RERANK_TOP_K,
                "rrf_k": settings.processing.KB_RRF_K,
                "final_k": args.final_k,
                "rerank_enabled": settings.processing.KB_RERANK_ENABLED,
            },
            "summary": summary,
            "rows": rows,
        }
        os.makedirs(os.path.dirname(os.path.abspath(args.report)) or ".", exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"\n报告已写入：{args.report}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生产索引上的 24 题逐级探针（只读）")
    parser.add_argument("--pool", type=int, default=20, help="每路召回数（生产 KB_HYBRID_SEARCH_TOP_K）")
    parser.add_argument("--rerank-n", default="3,5,10,20", help="重排窗口扫描，逗号分隔")
    parser.add_argument("--final-k", type=int, default=3, help="最终进上下文的块数（生产 top_k）")
    parser.add_argument(
        "--fold-table",
        action="store_true",
        help="融合后折叠同表行级块（与 KB_TABLE_FOLD_ENABLED 同一函数），用于验证改动效果",
    )
    parser.add_argument("--report", default="", help="把完整结果写成 JSON")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    args.rerank_n = [int(x) for x in str(args.rerank_n).split(",") if x.strip()]
    try:
        return asyncio.run(main_async(args))
    except Exception as exc:  # noqa: BLE001 - 顶层统一转退出码
        print(f"探针失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
