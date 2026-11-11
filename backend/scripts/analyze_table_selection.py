"""诊断「表格类问题」卡在哪一级：召回 / 重排 / 选择。

为什么需要这个脚本
------------------
「表格感知选择」（同表只占一席 + 整表回填）已经被提过两次，两次的结论都不对路：

- **整表回填**：概览 +2.7pp，但要 1.7 倍上下文，且救不回目标题；
- **同表只占一席**：−10.4pp，因为它把高分表压到 1 席之后，顶上来的候选只有
  0.0009 分——池子里根本没有更好的东西。

根因是这条链的**顺序**：正确答案先得被召回（进候选池），才谈得上被重排选中，
最后才谈得上「名额怎么分配」。跳过前两级直接改选择，只能换来噪声。
本脚本把三级各自的数字一次算清，免得下次再从「选择」开始猜。

三件事
------
1. **回填预算扫描**：不同 `cap_chars` 与回填范围下，概览/细节覆盖率与上下文成本；
2. **召回声位**：指定题的答案块在 BM25 里的名次（池子只有 20 时，名次 > 20 就是没戏）；
3. **重排对比**（可选 `--rerank`）：同一问题的「单行片段」与「整张表合并」谁分更高。

用法
----
    python scripts/analyze_table_selection.py \
        --probe ../docs/benchmarks/stage-probe-2026-11-10.json \
        --diagnose "监控哪些指标"

    # 需要重排对比时（模型缓存需就绪）
    HF_HOME=data/model_cache/huggingface python scripts/analyze_table_selection.py --rerank ...
"""

import argparse
import json
import os
import statistics
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)
sys.path.insert(0, os.path.join(BACKEND_DIR, "scripts"))

from probe_retrieval_stages import (  # noqa: E402
    DEFAULT_DOCS,
    _table_header_of,
    chunk_docs,
    coverage,
)


def load_corpus(chunk_size: int = 512, chunk_overlap: int = 64) -> Tuple[List[Dict], Dict[str, Dict]]:
    corpus = chunk_docs(DEFAULT_DOCS, chunk_size, chunk_overlap)
    return corpus, {c["id"]: c for c in corpus}


def refill(
    seeds: Sequence[str],
    corpus: Sequence[Dict],
    text_of,
    cap_chars: int,
    only_top1: bool = False,
) -> Tuple[List[str], int, int]:
    """把选中的表格行块补成整张表（同文档 + 同表头），返回 (ids, 字符数, 新增块数)。

    与探针里的同名函数保持一致，额外返回字符数以便直接看上下文成本。
    """
    base = list(seeds[:1]) if only_top1 else list(seeds)
    out = list(base)
    used = sum(len(text_of(i)) for i in out)
    added = 0
    for cid in base:
        header = _table_header_of(text_of(cid))
        if not header:
            continue
        doc_prefix = cid.split("#")[0] + "#"
        for other in corpus:
            if not other["id"].startswith(doc_prefix) or other["id"] in out:
                continue
            if _table_header_of(other["text"]) != header:
                continue
            if used + len(other["text"]) > cap_chars:
                continue
            out.append(other["id"])
            used += len(other["text"])
            added += 1
    return out, used, added


def sweep(
    rows: Dict[str, Dict], text_of, corpus: Sequence[Dict], caps: Sequence[int]
) -> List[Dict]:
    """回填预算扫描：每个 cap 下概览/细节的覆盖率与平均上下文字符数。"""
    results = []
    for only_top1 in (True, False):
        for cap in caps:
            entry = {"only_top1": only_top1, "cap": cap, "by_kind": {}}
            for kind in ("overview", "detail"):
                vals, chars, fired = [], [], 0
                for q, r in rows.items():
                    if r["kind"] != kind:
                        continue
                    seeds = [p["id"] for p in r["stages"]["rerank_n3"]["picked"]]
                    ids, used, added = refill(seeds, corpus, text_of, cap, only_top1)
                    fired += 1 if added else 0
                    vals.append(coverage([text_of(i) for i in ids], r["facts"])[0])
                    chars.append(used)
                entry["by_kind"][kind] = {
                    "coverage": round(statistics.mean(vals), 3) if vals else None,
                    "chars": round(statistics.mean(chars)) if chars else None,
                    "fired": fired,
                }
            results.append(entry)
    return results


def baseline(rows: Dict[str, Dict], text_of) -> Dict[str, Dict]:
    out = {}
    for kind in ("overview", "detail"):
        sub = [(q, r) for q, r in rows.items() if r["kind"] == kind]
        if not sub:
            continue
        out[kind] = {
            "coverage": round(
                statistics.mean(
                    coverage([text_of(p["id"]) for p in r["stages"]["rerank_n3"]["picked"]], r["facts"])[0]
                    for _q, r in sub
                ),
                3,
            ),
            "chars": round(
                statistics.mean(
                    sum(len(text_of(p["id"])) for p in r["stages"]["rerank_n3"]["picked"]) for _q, r in sub
                )
            ),
        }
    return out


def recall_rank(corpus: Sequence[Dict], question: str, targets: Sequence[str]) -> Optional[Dict]:
    """答案块在 BM25 里的名次——名次 > 通道候选数，就说明「压根没进池子」。

    只算 BM25 一路：它便宜、确定，且足以回答「召回够不够」。dense 侧要连 Ollama，
    本脚本不替它做决定（需要时用探针）。
    """
    from pymilvus.model.sparse.bm25 import BM25EmbeddingFunction
    from pymilvus.model.sparse.bm25.tokenizers import build_default_analyzer

    texts = [c["text"] for c in corpus]
    ids = [c["id"] for c in corpus]
    ef = BM25EmbeddingFunction(build_default_analyzer(language="zh"))
    ef.fit(texts)
    qv = ef.encode_queries([question])[0].toarray().flatten()
    scores = ef.encode_documents(texts).toarray() @ qv
    order = scores.argsort()[::-1]
    rank_of = {}
    for t in targets:
        if t not in ids:
            continue
        i = ids.index(t)
        rank_of[t] = {
            "score": round(float(scores[i]), 3),
            "rank": int((order == i).nonzero()[0][0]) + 1,
        }
    return {"question": question, "query_nnz": int((qv > 0).sum()), "ranks": rank_of, "total": len(ids)}


def rerank_compare(question: str, row_text: str, merged_text: str) -> Dict[str, float]:
    """同一问题下，「单行片段」与「整张表合并」的重排原始分对比。"""
    import math

    from sentence_transformers import CrossEncoder

    model = CrossEncoder("BAAI/bge-reranker-base", max_length=256, device="cpu")
    raw = model.predict([(question, row_text), (question, merged_text)], activation_fn=None)
    return {
        "row_raw": round(float(raw[0]), 4),
        "merged_raw": round(float(raw[1]), 4),
        "row_sigmoid": round(1 / (1 + math.exp(-float(raw[0]))), 4),
        "merged_sigmoid": round(1 / (1 + math.exp(-float(raw[1]))), 4),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="表格类问题的三级诊断")
    parser.add_argument("--probe", required=True, help="探针报告 JSON（提供题目、事实点与已选块）")
    parser.add_argument("--caps", default="800,1200,2000,3000", help="回填字符预算扫描")
    parser.add_argument("--diagnose", default="", help="按题目前缀诊断召回声位")
    parser.add_argument("--rerank", action="store_true", help="额外做单行 vs 整表的重排分对比")
    args = parser.parse_args(argv)

    data = json.loads(Path(args.probe).read_text(encoding="utf-8"))
    rows = {r["question"]: r for r in data.get("rows", []) if r.get("stages")}
    if not rows:
        print("探针报告里没有可用题目", file=sys.stderr)
        return 2

    corpus, _by_id = load_corpus()
    text_of = lambda i: _by_id[i]["text"]

    base = baseline(rows, text_of)
    print("基线（3 块，不回填）")
    for kind, v in base.items():
        print(f"  {kind:9} 覆盖 {v['coverage']:.3f}  上下文字符 {v['chars']}")

    print("\n回填预算扫描")
    print(f"  {'范围':10}{'cap':>6}{'概览':>7}{'细节':>7}{'字符':>7}{'触发':>8}")
    for e in sweep(rows, text_of, corpus, [int(c) for c in args.caps.split(",")]):
        o = e["by_kind"]["overview"]
        d = e["by_kind"]["detail"]
        label = "仅top1" if e["only_top1"] else "三块"
        fired = f"{o['fired']}+{d['fired']}"
        chars = round((o["chars"] + d["chars"]) / 2)
        print(f"  {label:10}{e['cap']:>6}{o['coverage']:>7.3f}{d['coverage']:>7.3f}{chars:>7}{fired:>8}")

    if args.diagnose:
        hit = next((q for q in rows if q.startswith(args.diagnose)), None)
        if not hit:
            print(f"\n没找到以「{args.diagnose}」开头的题", file=sys.stderr)
            return 2
        facts = rows[hit]["facts"]
        targets = [c["id"] for c in corpus if any(f in c["text"] for f in facts)]
        info = recall_rank(corpus, hit, targets)
        print(f"\n召回声位：{hit}")
        print(f"  查询 BM25 非零维：{info['query_nnz']}；语料 {info['total']} 块")
        for cid, r in info["ranks"].items():
            flag = "（超出通道候选 20，进不了池子）" if r["rank"] > 20 else ""
            print(f"  {cid:<24} BM25 分 {r['score']:>6.3f} 名次 {r['rank']:>3}{flag}")

        if args.rerank and targets:
            t0 = [_by_id[i] for i in targets if _table_header_of(_by_id[i]["text"])]
            if t0:
                header = _table_header_of(t0[0]["text"])
                merged = "\n".join(
                    [c["text"].split("\n")[0] for c in t0[:1]] + [header]
                    + [c["text"].split("\n")[-1] for c in corpus
                       if _table_header_of(c["text"]) == header]
                )
                scores = rerank_compare(hit, t0[0]["text"], merged)
                print(f"\n重排对比（{len([c for c in corpus if _table_header_of(c['text']) == header])} 行合并）")
                print(f"  单行 raw={scores['row_raw']:+.3f} / 整表 raw={scores['merged_raw']:+.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
