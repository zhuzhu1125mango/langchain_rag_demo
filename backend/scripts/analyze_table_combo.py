"""表格感知选择的**组合**验证：扩通道池 → 融合候选上折叠同表 → 放开重排窗口。

## 为什么需要这个脚本

11-11 的三级诊断（`analyze_table_selection.py`）把「监控哪些指标、有哪些告警」的
失败定位在前两级，不是选择级：

- 召回：告警行（`ScrapeTargetDown`）BM25 名次 **20**，`PostgresDatabaseDown` **64**，
  处置速查 **51** —— 而通道候选只要 20 条，后两条结构上进不了池子；
- 重排：该行 raw 0.788，整表合并后 0.697（更低），而指标表行 0.970。

探针里已测过的两个动作（同表折叠 0.452、整表回填 0.583）都只改了**选择层**，
它们共用的前提是「池子里已经有正确答案」。这个前提不成立，所以两个动作都不成立。

于是剩下唯一有机制的组合是三件事同时发生，缺一不可：

1. **扩池**：通道候选 20 → 更大，让 BM25 名次 64 的告警行进融合池；
2. **在融合候选上折叠**（而不是在已选出的 3 块上折叠）：同一张表的行只留 1 个代表，
   否则扩池后指标表的更多行会占满窗口，等于把池子扩给了同一张表；
3. **放开重排窗口**：折叠后仍只取 3 条送重排就白扩了。

单用任何一条都已被数据否过（窗口 20 单独是 −10.5pp）。这个脚本只回答一个问题：
**三者叠加，概览覆盖能不能显著超过基线**。

## 口径

与 `probe_retrieval_stages.py` 完全一致：同一批 4 篇文档、同一个生产切分器
（512/64）、同一批 24 题、同一个 RRF（k=60）、同一个 bge-reranker-base（max_length=256）。
baseline 一栏必须复现 0.556 / 0.792，否则说明脚本口径漂了，其余数字不可信。

## 用法

    cd backend
    .venv/Scripts/python.exe scripts/analyze_table_combo.py --no-rerank          # 只看召回声位，秒级
    .venv/Scripts/python.exe scripts/analyze_table_combo.py                       # 完整（含重排，数分钟）
    .venv/Scripts/python.exe scripts/analyze_table_combo.py --pools 20,40,64,100 --report ../docs/benchmarks/table-combo.json

不进 CI：依赖本机 Ollama 与 1GB 级重排模型。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from probe_retrieval_stages import (  # noqa: E402
    DEFAULT_DOCS,
    QUESTIONS,
    _table_header_of,
    chunk_docs,
    coverage,
)
from run_channel_ab import (  # noqa: E402
    DEFAULT_EMBED_MODEL,
    DEFAULT_OLLAMA,
    DEFAULT_RERANK_MODEL,
    build_reranker,
    cosine_rank_scored,
    embed_texts,
)
from run_eval import build_index, rank_with_index  # noqa: E402
from run_multiquery_ab import RRF_K, rrf_fuse  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# 目标题：它的失败机制最清楚（同表三行占满窗口、告警行名次 20/64）
TARGET_PREFIX = "监控哪些指标"


def is_doc_lead(cid: str, corpus_by_id: Dict[str, Dict], max_chars: int = 400) -> bool:
    """是否是文档开头的元信息块（只有标题、定位、配置路径、更新时间，没有任何答案）。

    这类块对「监控有哪些告警」这类问题字面相似度极高（标题就叫「监控运维指南」），
    重排会给 0.81 的高分，但它一个字都答不出来。它与「目录块」不同：目录块是
    `1. [环境要求](#1-环境要求)` 那种链接列表，这个只有标题和元数据。
    """
    doc = corpus_by_id[cid]
    # heading_path 没有子节 → 处于文档标题层
    if " > " in doc["heading_path"]:
        return False
    text = doc["text"]
    return len(text) <= max_chars and not any(ln.strip().startswith(("|", "- ", "* ")) for ln in text.splitlines())


def fold_same_table(fused: Sequence[str], corpus_by_id: Dict[str, Dict]) -> List[str]:
    """在**融合候选**上折叠：同一 (文档, 表头) 的行级表格块只保留第一个。

    只在融合顺序上收敛，不看重排分 —— 这一步发生在重排之前，此时还没有分数。
    非表格块原样保留（它们的「重复」是正当的相邻证据，例如优雅停机的连续步骤）。
    """
    seen: set[Tuple[str, str]] = set()
    out: List[str] = []
    for cid in fused:
        header = _table_header_of(corpus_by_id[cid]["text"])
        if header:
            key = (cid.split("#")[0], header)
            if key in seen:
                continue
            seen.add(key)
        out.append(cid)
    return out


class RerankCache:
    """按 (问题, 片段) 缓存重排分：多个 pool / 折叠组合共用同一次打分。"""

    def __init__(self, predict):
        self._predict = predict
        self._cache: Dict[Tuple[str, str], float] = {}

    def fill(self, question: str, ids: Sequence[str], corpus_by_id: Dict[str, Dict]) -> None:
        missing = [i for i in ids if (question, i) not in self._cache]
        if not missing:
            return
        scores = self._predict(question, [corpus_by_id[i]["text"] for i in missing])
        for cid, score in zip(missing, scores):
            self._cache[(question, cid)] = float(score)

    def score(self, question: str, cid: str) -> float:
        return self._cache.get((question, cid), 0.0)


def rank_fused(
    fused: Sequence[str],
    question: str,
    corpus_by_id: Dict[str, Dict],
    window: int,
    final_k: int,
    rerank: RerankCache | None,
) -> List[str]:
    """在融合候选的前 `window` 条里选出 `final_k` 块（有重排则按重排分，否则按融合序）。

    打分必须在排序**之前**补齐，否则排序读到的是全 0 的默认值 —— 那等于「按原顺序取」，
    会把「重排没帮上忙」误读成「重排没用」。这个顺序是脚本唯一的易错点，单独提出来。
    """
    cands = list(fused[:window])
    if rerank is not None:
        rerank.fill(question, cands, corpus_by_id)
        cands.sort(key=lambda c: rerank.score(question, c), reverse=True)
    return cands[:final_k]


def run_config(
    label: str,
    pool: int,
    fold: bool,
    window: int,
    final_k: int,
    sparse_all: Dict[str, List[str]],
    dense_all: Dict[str, List[str]],
    corpus_by_id: Dict[str, Dict],
    rerank: RerankCache | None,
    drop_lead: bool = False,
) -> Dict:
    rows = []
    for item in QUESTIONS:
        q, facts = item["q"], item["facts"]
        fused = rrf_fuse([sparse_all[q][:pool], dense_all[q][:pool]], k=RRF_K)
        if drop_lead:
            fused = [c for c in fused if not is_doc_lead(c, corpus_by_id)]
        if fold:
            fused = fold_same_table(fused, corpus_by_id)
        picked = rank_fused(fused, q, corpus_by_id, window, final_k, rerank)
        frac, hits = coverage([corpus_by_id[c]["text"] for c in picked], facts)
        rows.append(
            {
                "question": q,
                "kind": item["kind"],
                "coverage": round(frac, 2),
                "hits": hits,
                "picked": picked,
                "n_fused_after_fold": len(fused),
            }
        )
    by_kind = {}
    for kind in ("overview", "detail"):
        sub = [r for r in rows if r["kind"] == kind]
        by_kind[kind] = round(statistics.mean(r["coverage"] for r in sub), 3)
    return {"label": label, "pool": pool, "fold": fold, "window": window, "by_kind": by_kind, "rows": rows}


def diagnose_pool(
    pool: int,
    sparse_all: Dict[str, List[str]],
    dense_all: Dict[str, List[str]],
    corpus_by_id: Dict[str, Dict],
    rerank: RerankCache | None,
) -> None:
    """对目标题打印「事实块在各阶段的名次」，用来判断扩池到底有没有把货捞进来。"""
    item = next((x for x in QUESTIONS if x["q"].startswith(TARGET_PREFIX)), None)
    if item is None:
        return
    q, facts = item["q"], item["facts"]
    fused = rrf_fuse([sparse_all[q][:pool], dense_all[q][:pool]], k=RRF_K)
    folded = fold_same_table(fused, corpus_by_id)

    def first_fact_rank(seq: Sequence[str]) -> int | None:
        for i, cid in enumerate(seq, start=1):
            if coverage([corpus_by_id[cid]["text"]], facts)[0] > 0:
                return i
        return None

    print(f"\n  [诊断] 「{q}」pool={pool}")
    print(f"    sparse 名次（事实块）: {first_fact_rank(sparse_all[q][:pool]) or '—'} / 池 {pool}")
    print(f"    dense  名次（事实块）: {first_fact_rank(dense_all[q][:pool]) or '—'} / 池 {pool}")
    print(f"    融合后 {first_fact_rank(fused)} / {len(fused)} 条；折叠后 {first_fact_rank(folded)} / {len(folded)} 条")
    if rerank is not None:
        top = sorted(folded[:20], key=lambda c: rerank.score(q, c), reverse=True)[:5]
        print("    折叠后前 20 重排 top5：")
        for cid in top:
            print(f"      {rerank.score(q, cid):>7.4f}  {cid:<22} {corpus_by_id[cid]['heading_path'][-22:]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="表格感知选择：扩池 / 融合候选折叠 / 放开窗口 的组合验证")
    parser.add_argument("--docs", nargs="*", type=Path, default=DEFAULT_DOCS)
    parser.add_argument("--chunk-size", type=int, default=512)
    parser.add_argument("--chunk-overlap", type=int, default=64)
    parser.add_argument("--pools", default="20,40,64", help="通道候选数扫描（生产是 20）")
    parser.add_argument("--windows", default="20", help="重排窗口扫描（生产是 3）")
    parser.add_argument("--final-k", type=int, default=3)
    parser.add_argument("--no-rerank", action="store_true", help="只按融合顺序取，不跑重排模型")
    parser.add_argument("--ollama", default=DEFAULT_OLLAMA)
    parser.add_argument("--embed-model", default=DEFAULT_EMBED_MODEL)
    parser.add_argument("--rerank-model", default=DEFAULT_RERANK_MODEL)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    pools = [int(x) for x in args.pools.split(",") if x.strip()]
    windows = [int(x) for x in args.windows.split(",") if x.strip()]
    widest = max(pools)
    paths = [p if p.is_absolute() else BASE_DIR / p for p in args.docs]

    t0 = time.perf_counter()
    corpus = chunk_docs(paths, args.chunk_size, args.chunk_overlap)
    corpus_by_id = {doc["id"]: doc for doc in corpus}
    print(f"[combo] 语料 {len(corpus)} chunks（{args.chunk_size}/{args.chunk_overlap}）/ {time.perf_counter() - t0:.1f}s")

    t0 = time.perf_counter()
    bm25_ef, doc_matrix = build_index(corpus)
    sparse_all = {
        item["q"]: [cid for cid, _ in rank_with_index(bm25_ef, doc_matrix, corpus, item["q"], widest)]
        for item in QUESTIONS
    }
    print(f"[combo] sparse 池 {widest} / {time.perf_counter() - t0:.1f}s")

    t0 = time.perf_counter()
    doc_vectors = embed_texts([f"{d['title']} {d['text']}" for d in corpus], model=args.embed_model, base_url=args.ollama)
    q_vectors = embed_texts([item["q"] for item in QUESTIONS], model=args.embed_model, base_url=args.ollama)
    ids = [d["id"] for d in corpus]
    dense_all = {
        item["q"]: [cid for cid, _ in cosine_rank_scored(doc_vectors, qv, ids, widest)]
        for item, qv in zip(QUESTIONS, q_vectors)
    }
    print(f"[combo] dense 池 {widest} / {time.perf_counter() - t0:.1f}s")

    rerank = None
    if not args.no_rerank:
        t0 = time.perf_counter()
        predict = build_reranker(args.rerank_model, max_length=256)
        rerank = RerankCache(predict)
        # 先把所有组合要用到的候选一次打完分，后面各配置复用（同一对不重复推理）
        widest_window = max(windows)
        for pool in pools:
            for item in QUESTIONS:
                q = item["q"]
                fused = rrf_fuse([sparse_all[q][:pool], dense_all[q][:pool]], k=RRF_K)
                no_lead = [c for c in fused if not is_doc_lead(c, corpus_by_id)]
                for variant in (fused,
                                fold_same_table(fused, corpus_by_id),
                                no_lead,
                                fold_same_table(no_lead, corpus_by_id)):
                    rerank.fill(q, variant[:widest_window], corpus_by_id)
        print(f"[combo] rerank 预打分 {len(rerank._cache)} 对 / {time.perf_counter() - t0:.1f}s")

    configs = []
    for pool in pools:
        for window in windows:
            for fold in (False, True):
                for drop_lead in (False, True):
                    if window != min(windows) and not fold and not drop_lead:
                        continue  # 大窗口只用来看「折叠/drop 能不能救回它」，不必重复跑原样
                    label = f"pool{pool}/{'折叠' if fold else '原样'}/{'去元信息块' if drop_lead else '含元信息块'}/win{window}"
                    configs.append(
                        run_config(label, pool, fold, window, args.final_k,
                                   sparse_all, dense_all, corpus_by_id, rerank, drop_lead)
                    )

    print(f"\n{'配置':<28}{'概览(12)':>10}{'细节(12)':>10}  差异题（vs pool20/不折叠）")
    print("-" * 88)
    base = configs[0]
    base_map = {r["question"]: r["coverage"] for r in base["rows"]}
    for cfg in configs:
        changed = [
            f"{r['question'][:12]}({base_map[r['question']]:.2f}→{r['coverage']:.2f})"
            for r in cfg["rows"]
            if abs(r["coverage"] - base_map[r["question"]]) > 1e-9
        ]
        note = "  ".join(changed[:4]) if changed else "—"
        print(f"{cfg['label']:<28}{cfg['by_kind']['overview']:>10.3f}{cfg['by_kind']['detail']:>10.3f}  {note}")

    for pool in pools:
        diagnose_pool(pool, sparse_all, dense_all, corpus_by_id, rerank)

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps({"configs": configs, "n_chunks": len(corpus), "rrf_k": RRF_K}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n[combo] 报告已写入 {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
