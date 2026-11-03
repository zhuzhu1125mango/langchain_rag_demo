"""检索逐级探针（P1-8）：概览型问题到底丢在哪一级？

背景：端到端验证（docs/e2e-verification-2026-11-03.md）发现「问题越大越差」——
细节型全对，概览型（系统架构 / 优雅停机 / 部署步骤 / 监控告警）会给出自信的错答案。
当时的解释是「rerank 对宽泛问句不敏感」，但**这个解释没有证据支撑**：
生产 trace 只存了 rerank 之后的 top-3（`KB_HYBRID_RERANK_TOP_K=3`），
而候选池里有 20 条 —— 我们看到的 0.077/0.009 是「3 条候选里最好的」，
不是「20 条候选里最好的」。两种故障的修法完全相反：

  ① 候选池里就没有正确片段 → 改召回（embedding / 切分 / 通道）
  ② 候选池里有、被别人挤到 3 名之外 → 改融合或加宽重排窗口（`KB_HYBRID_RERANK_TOP_K`）
  ③ 候选池里有、也进了重排窗口、分仍很低 → 才是「重排对宽泛问句不敏感」

这个脚本用**真实文档 + 真实问题**把三级摊开看，并在每一级用「事实点覆盖率」
（答案被拆成 N 个必须出现的事实串，top-3 拼起来覆盖了几个）打分——概览型的
失败形态本来就是「拼不全」，用 hit@k 量不出来（文档命中但只命中一个片段也算 1.0）。

用法（本机需 `ollama serve` + bge-m3，重排模型走本地 HF 缓存）：
    cd backend && uv run python scripts/probe_retrieval_stages.py
    uv run python scripts/probe_retrieval_stages.py --rerank-n 3,5,10,20 --report ../docs/benchmarks/stage-probe.json
    uv run python scripts/probe_retrieval_stages.py --skip-rerank      # 先看召回与融合

本脚本**不进 CI**：依赖本机 Ollama 与 1GB 级重排模型，一次几十秒到几分钟。
CI 的卡点仍是 run_eval.py（纯离线 BM25）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
# 后端根目录：脚本要直接用生产的切分器（src.services.chunking），不能另写一套
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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

BASE_DIR = Path(__file__).resolve().parent.parent.parent  # 仓库根目录

# markdown 目录行：`1. [环境要求](#1-环境要求)`
_TOC_LINE_RE = re.compile(r"^\s*\d+\.\s*\[[^\]]+\]\(#")

# 与端到端验证同一批真实文档（生产 CHUNK_SIZE=512 / OVERLAP=64 → 共 502 个 chunk）
DEFAULT_DOCS = [
    BASE_DIR / "docs" / "architecture.md",
    BASE_DIR / "docs" / "guide" / "deployment.md",
    BASE_DIR / "docs" / "guide" / "backup-restore.md",
    BASE_DIR / "docs" / "guide" / "monitoring.md",
]

# 问题与「答案必须包含的事实串」。事实串取自文档原文，且**按 chunk 命中数筛选**
# （df ≤ 8）：像 "Milvus"(54)、"端口"(56)、"校验"(23) 这类词几乎每块都有，
# 拿它们当事实点等于没量——随便捞三块就「覆盖」了。502 个 chunk 下 df=8 只占 1.6%，
# 而 df=54 意味着每条候选都有约 11% 概率白送一个「命中」。覆盖率要能反映
# 「这一点答到了」，所以宁可每题只留 1~4 个低频串。
# 概览型的失败形态是「拼不全」，不是「一篇都没捞到」，用覆盖率而不用 hit@k 才能量出来。
#
# 题量 24（概览 12 / 细节 12）：10 道题时 1 题 = 10pp，任何差异都读不出显著性；
# 24 道时 1 题 ≈ 4.2pp，才能分辨「章节级召回」这种量级的改动。
QUESTIONS: List[Dict] = [
    # ---------------- 概览型：答案跨章节 ----------------
    {
        "q": "系统由哪些部分组成，请求怎么流转？",
        "kind": "overview",
        "facts": ["intent_router", "context_builder", "citation_backfiller", "output_sanitizer"],
    },
    {
        "q": "优雅停机怎么做？为什么需要？",
        "kind": "overview",
        "facts": ["SIGTERM", "timeout-graceful-shutdown", "SIGKILL", "drain_invalidations"],
    },
    {
        "q": "监控哪些指标、有哪些告警？",
        "kind": "overview",
        "facts": ["ScrapeTargetDown", "PostgresDatabaseDown"],
    },
    {
        "q": "备份怎么恢复？恢复后确认什么？",
        "kind": "overview",
        "facts": ["pg_dump", "pg_stat_activity", "恢复顺序"],
    },
    {
        "q": "从零部署一套生产环境要做哪些事？",
        "kind": "overview",
        "facts": ["首次部署准备", "强密码", "start-prod.sh"],
    },
    {
        "q": "出问题要回滚，怎么决策、怎么执行？",
        "kind": "overview",
        "facts": ["决策矩阵", "应用回退", "alembic downgrade"],
    },
    {
        "q": "容量怎么规划，什么时候该扩容？",
        "kind": "overview",
        "facts": ["规模参考", "扩容触发阈值"],
    },
    {
        "q": "告警怎么通知出去，怎么配接收人？",
        "kind": "overview",
        "facts": ["alertmanager.yml", "webhook"],
    },
    {
        "q": "备份和恢复的 RPO / RTO 目标是多少？",
        "kind": "overview",
        "facts": ["RPO", "RTO"],
    },
    {
        "q": "一致性巡检怎么跑，结果怎么判读？",
        "kind": "overview",
        "facts": ["退出码"],
    },
    {
        "q": "数据库迁移出错和应用版本出错，回滚方式有什么不同？",
        "kind": "overview",
        "facts": ["alembic downgrade", "应用回退"],
    },
    {
        "q": "生产环境升级换版本要注意什么？",
        "kind": "overview",
        "facts": ["SIGTERM", "应用回退"],
    },
    # ---------------- 细节型：答案集中在某一段 ----------------
    {
        "q": "为什么要给 NLTK 语料单独放进镜像？",
        "kind": "detail",
        "facts": ["RuntimeWarning", "NLTK_DATA"],
    },
    {
        "q": "ScrapeTargetDown 是什么意思？",
        "kind": "detail",
        "facts": ["ScrapeTargetDown"],
    },
    {
        "q": "改完告警规则怎么生效？",
        "kind": "detail",
        "facts": ["promtool", "reload"],
    },
    {
        "q": "数据库迁移怎么执行？",
        "kind": "detail",
        "facts": ["alembic upgrade head", "alembic check"],
    },
    {
        "q": "恢复的顺序为什么不能打乱？",
        "kind": "detail",
        "facts": ["恢复顺序", "不可打乱"],
    },
    {
        "q": "只想恢复某一个知识库，怎么部分恢复？",
        "kind": "detail",
        "facts": ["部分恢复", "单知识库"],
    },
    {
        "q": "数据库迁移怎么回滚？",
        "kind": "detail",
        "facts": ["alembic downgrade"],
    },
    {
        "q": "熔断触发的时间和条件是什么？",
        "kind": "detail",
        "facts": ["60s"],
    },
    {
        "q": "流式响应的事件是怎么发给前端的？",
        "kind": "detail",
        "facts": ["data:", "event:"],
    },
    {
        "q": "换 Embedding 模型需要做什么？",
        "kind": "detail",
        "facts": ["embedding_migration"],
    },
    {
        "q": "部署生产环境前必须配哪些密码？",
        "kind": "detail",
        "facts": ["强密码"],
    },
    {
        "q": "备份恢复后要确认哪些事？",
        "kind": "detail",
        "facts": ["恢复后必做"],
    },
]


# ---------------------------------------------------------------------------
# 语料：用生产同一套切分器（含 heading_path 前置），而不是手工拼段落
# ---------------------------------------------------------------------------

def chunk_docs(paths: Sequence[Path], chunk_size: int, chunk_overlap: int) -> List[Dict]:
    """按生产切分策略切分真实文档，返回 [{id, title, text, heading_path}]。"""
    from langchain_core.documents import Document

    from src.services.chunking import ChunkingFactory

    corpus: List[Dict] = []
    for path in paths:
        raw = path.read_text(encoding="utf-8")
        docs = [Document(page_content=raw, metadata={"source": str(path)})]
        chunks = ChunkingFactory.split(
            docs,
            file_path=str(path),
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
        for i, chunk in enumerate(chunks):
            corpus.append(
                {
                    "id": f"{path.name}#{i}",
                    "title": path.stem,
                    "text": chunk.page_content,
                    "heading_path": chunk.metadata.get("heading_path", ""),
                }
            )
    return corpus


def fact_document_frequency(corpus: Sequence[Dict], facts: Sequence[str]) -> Dict[str, int]:
    """每个事实串出现在多少个 chunk 里——覆盖率的可信度取决于它。

    「Milvus」这类词在几十个 chunk 里都有，拿它当事实点等于没量：
    随便捞三个块就「覆盖」了。df 大的串只说明「这个词在文档里」，
    不说明「答到了这一点」，所以打印出来让人自己判断，而不是悄悄算进分数。
    """
    return {f: sum(1 for doc in corpus if f in doc["text"]) for f in facts}


def coverage(texts: Sequence[str], facts: Sequence[str]) -> Tuple[float, List[str]]:
    """事实点覆盖率：facts 里有几个出现在拼接后的文本中。返回 (比例, 命中的事实)。"""
    blob = "\n".join(texts)
    hit = [f for f in facts if f in blob]
    return (len(hit) / len(facts) if facts else 0.0), hit


def is_toc_chunk(text: str) -> bool:
    """是否是「目录」型片段：连续 3 行以上是 `1. [标题](#锚点)`。

    这类块把全文所有标题塞进一个 chunk，对任何问题都「极度相关」——
    实测「生产环境怎么部署」它拿 0.9556 进 top-3，但一个字的答案都没有。
    它是检索的纯干扰项，且只在 markdown 文档出现。
    """
    lines = [ln for ln in text.splitlines() if _TOC_LINE_RE.match(ln)]
    return len(lines) >= 3


def expand_ids(
    doc_id: str,
    corpus_by_id: Dict[str, Dict],
    radius: int,
    same_heading_only: bool,
) -> List[str]:
    """按 chunk_index 邻接扩展（模拟「选中块 → 回填邻块」的小-大检索）。

    生产侧不需要重灌数据就能做：Milvus 已返回 document_id 与 chunk_index，
    按同文档相邻下标回查即可。`same_heading_only` 是安全阀——跨标题扩展
    容易把无关章节卷进上下文，与「少而准」的目标相反。
    """
    name, _, idx = doc_id.rpartition("#")
    if doc_id not in corpus_by_id:
        return [doc_id]
    try:
        center = int(idx)
    except ValueError:
        return [doc_id]
    base_heading = corpus_by_id[doc_id]["heading_path"]
    out: List[str] = []
    for delta in range(-radius, radius + 1):
        cand = corpus_by_id.get(f"{name}#{center + delta}")
        if cand is None:
            continue
        if same_heading_only and delta != 0 and cand["heading_path"] != base_heading:
            continue
        out.append(f"{name}#{center + delta}")
    return out


def best_fact_rank(ranked: Sequence[str], corpus_by_id: Dict[str, Dict], facts: Sequence[str]) -> Tuple[int | None, float]:
    """返回 (第一个含任一事实的片段名次, 该片段的覆盖率)；没有则 (None, 0.0)。

    名次从 1 开始。用它回答「候选池里到底有没有正确片段」——比看分数可靠，
    因为 RRF 分与 rerank 分的量纲不可比。
    """
    for rank, doc_id in enumerate(ranked, start=1):
        frac, _ = coverage([corpus_by_id[doc_id]["text"]], facts)
        if frac > 0:
            return rank, frac
    return None, 0.0


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="检索逐级探针：召回 / 融合 / 重排各自丢了什么")
    parser.add_argument("--docs", nargs="*", type=Path, default=DEFAULT_DOCS)
    parser.add_argument("--chunk-size", type=int, default=512, help="与生产 CHUNK_SIZE 一致")
    parser.add_argument("--chunk-overlap", type=int, default=64, help="与生产 CHUNK_OVERLAP 一致")
    parser.add_argument("--channel-top-k", type=int, default=20, help="每路召回数（生产 KB_HYBRID_SEARCH_TOP_K）")
    parser.add_argument("--final-k", type=int, default=3, help="最终进上下文的片段数（生产 top_k）")
    parser.add_argument("--rerank-n", default="3,5,10,20", help="重排窗口候选数扫描（逗号分隔）")
    parser.add_argument("--expand", type=int, default=1, help="邻块回填半径（0 = 不回填）")
    parser.add_argument(
        "--expand-cross-heading",
        action="store_true",
        help="允许跨标题回填邻块（默认只在同一 heading 内回填）",
    )
    parser.add_argument("--skip-rerank", action="store_true")
    parser.add_argument("--ollama", default=DEFAULT_OLLAMA)
    parser.add_argument("--embed-model", default=DEFAULT_EMBED_MODEL)
    parser.add_argument("--rerank-model", default=DEFAULT_RERANK_MODEL)
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()

    rerank_ns = [int(x) for x in args.rerank_n.split(",") if x.strip()]
    paths = [p if p.is_absolute() else BASE_DIR / p for p in args.docs]

    t0 = time.perf_counter()
    corpus = chunk_docs(paths, args.chunk_size, args.chunk_overlap)
    corpus_by_id = {doc["id"]: doc for doc in corpus}
    print(f"[probe] 语料 {len(corpus)} 个 chunk（{args.chunk_size}/{args.chunk_overlap}）"
          f" / 耗时 {time.perf_counter() - t0:.1f}s")
    for path in paths:
        n = sum(1 for doc in corpus if doc["id"].startswith(f"{path.name}#"))
        print(f"        {path.name:<22} {n:>4} chunks")

    # --- 通道 1/2：sparse（BM25，fit 一次）---
    t0 = time.perf_counter()
    bm25_ef, doc_matrix = build_index(corpus)
    sparse_rank: Dict[str, List[str]] = {}
    for item in QUESTIONS:
        rows = rank_with_index(bm25_ef, doc_matrix, corpus, item["q"], args.channel_top_k)
        sparse_rank[item["q"]] = [doc_id for doc_id, _ in rows]
    print(f"[probe] sparse 索引+查询 {time.perf_counter() - t0:.1f}s")

    # --- 通道 3：dense（Ollama bge-m3，带磁盘缓存）---
    t0 = time.perf_counter()

    def _embed(texts: List[str]) -> List[List[float]]:
        return embed_texts(texts, model=args.embed_model, base_url=args.ollama)

    doc_vectors = _embed([f"{doc['title']} {doc['text']}" for doc in corpus])
    q_vectors = _embed([item["q"] for item in QUESTIONS])
    ids = [doc["id"] for doc in corpus]
    dense_rank: Dict[str, List[str]] = {
        item["q"]: [doc_id for doc_id, _ in cosine_rank_scored(doc_vectors, qv, ids, args.channel_top_k)]
        for item, qv in zip(QUESTIONS, q_vectors)
    }
    print(f"[probe] dense 召回 {time.perf_counter() - t0:.1f}s")

    # 事实串的 chunk 命中数：df 越小，覆盖率越能代表「这一点答到了」
    df_table = fact_document_frequency(corpus, [f for item in QUESTIONS for f in item["facts"]])
    common = {f: n for f, n in df_table.items() if n > 8}
    if common:
        print(f"[probe] 注意：以下事实串在 >3 个 chunk 中出现，覆盖率会被灌水 —— {common}")

    # --- 融合：RRF（与生产同 k=60、等权）---
    fused_rank: Dict[str, List[str]] = {
        item["q"]: rrf_fuse([sparse_rank[item["q"]], dense_rank[item["q"]]], k=RRF_K)
        for item in QUESTIONS
    }

    # --- 重排：bge-reranker-base（生产 KB_RERANK_MAX_LENGTH=256 / CPU）---
    rerank_scores: Dict[str, Dict[str, float]] = {}
    rerank_time: Dict[int, float] = {}
    predict = None if args.skip_rerank else build_reranker(args.rerank_model, max_length=256)
    if predict is not None:
        widest = max(rerank_ns)
        t0 = time.perf_counter()
        for item in QUESTIONS:
            cands = fused_rank[item["q"]][:widest]
            scores = predict(item["q"], [corpus_by_id[c]["text"] for c in cands])
            rerank_scores[item["q"]] = dict(zip(cands, scores))
        total = time.perf_counter() - t0
        rerank_time[widest] = total
        print(f"[probe] rerank 全部题 × 窗口 {widest} = {total:.1f}s（平均 {total / len(QUESTIONS):.2f}s/题）")

    # --- 逐题摊开 ---
    rows: List[Dict] = []
    for item in QUESTIONS:
        q, facts = item["q"], item["facts"]
        dense_best, dense_frac = best_fact_rank(dense_rank[q], corpus_by_id, facts)
        sparse_best, sparse_frac = best_fact_rank(sparse_rank[q], corpus_by_id, facts)
        fused_best, fused_frac = best_fact_rank(fused_rank[q], corpus_by_id, facts)

        row: Dict = {
            "question": q,
            "kind": item["kind"],
            "facts": facts,
            "valid": item.get("valid", True),
            "note": item.get("note", ""),
            "n_chunks": len(corpus),
            # 名次：候选池里第一个含答案片段的位置；None = 这一级就没捞到
            "sparse_rank": sparse_best,
            "dense_rank": dense_best,
            "fused_rank": fused_best,
            "fused_best_coverage": round(fused_frac, 2),
            "stages": {},
        }

        if rerank_scores:
            for n in rerank_ns:
                window = fused_rank[q][:n]
                scored = sorted(
                    ((c, rerank_scores[q].get(c, 0.0)) for c in window),
                    key=lambda kv: kv[1],
                    reverse=True,
                )
                picked = [c for c, _ in scored[: args.final_k]]
                frac, hits = coverage([corpus_by_id[c]["text"] for c in picked], facts)
                row["stages"][f"rerank_n{n}"] = {
                    "coverage": round(frac, 2),
                    "top1_score": round(scored[0][1], 4) if scored else 0.0,
                    # 选中了什么：复盘「高分块到底是不是答案」必须看这个，
                    # 只有分数的话会得出「模型不敏感」这种没有机制的解释
                    "picked": [
                        {
                            "id": c,
                            "score": round(s, 4),
                            "heading_path": corpus_by_id[c]["heading_path"],
                            "preview": corpus_by_id[c]["text"][:160].replace("\n", " ⏎ "),
                        }
                        for c, s in scored[: args.final_k]
                    ],
                    # 该窗口里答案片段的**最好重排名次**：判断「重排是否认得它」
                    "answer_rerank_rank": next(
                        (i for i, (c, _) in enumerate(scored, start=1)
                         if coverage([corpus_by_id[c]["text"]], facts)[0] > 0),
                        None,
                    ),
                    "facts_hit": hits,
                }

            # 变体 A：重排窗口**剔除目录块**（模拟生产侧过滤 TOC）
            for n in rerank_ns:
                window = [c for c in fused_rank[q] if not is_toc_chunk(corpus_by_id[c]["text"])][:n]
                scored = sorted(
                    ((c, rerank_scores[q].get(c, 0.0)) for c in window),
                    key=lambda kv: kv[1],
                    reverse=True,
                )
                picked = [c for c, _ in scored[: args.final_k]]
                frac, _ = coverage([corpus_by_id[c]["text"] for c in picked], facts)
                row["stages"][f"notoc_n{n}"] = {"coverage": round(frac, 2), "picked": [c for c in picked]}

            # 变体 B：top-3 选中块做**邻块回填**（小-大检索，模拟生产侧上下文扩展）
            prod_n = min(rerank_ns)
            seed = [p["id"] for p in row["stages"][f"rerank_n{prod_n}"]["picked"]]
            expanded: List[str] = []
            for cid in seed:
                for c2 in expand_ids(cid, corpus_by_id, args.expand, not args.expand_cross_heading):
                    if c2 not in expanded:
                        expanded.append(c2)
            frac, _ = coverage([corpus_by_id[c]["text"] for c in expanded], facts)
            row["stages"]["expand"] = {
                "coverage": round(frac, 2),
                "n_chunks": len(expanded),
                "chars": sum(len(corpus_by_id[c]["text"]) for c in expanded),
                "ids": expanded,
            }

            # 变体 C：**去冗余**——同一 heading_path 最多占 1 个名额，再补齐
            # 动机：监控题 top-3 是同一张表的三行（0.97/0.9693/0.9678），
            # 三个名额换三行表格，而「有哪些告警」在另一张表里，永远轮不上。
            # 这不是「不相关」，是「相关但重复」——排序正确、**组合**错误。
            window = fused_rank[q][: max(rerank_ns)]
            ranked_all = sorted(
                ((c, rerank_scores[q].get(c, 0.0)) for c in window),
                key=lambda kv: kv[1],
                reverse=True,
            )
            seen_headings, picked_div = set(), []
            for c, _s in ranked_all:
                h = corpus_by_id[c]["heading_path"]
                if h in seen_headings:
                    continue
                seen_headings.add(h)
                picked_div.append(c)
                if len(picked_div) == args.final_k:
                    break
            frac, _ = coverage([corpus_by_id[c]["text"] for c in picked_div], facts)
            row["stages"]["diverse"] = {
                "coverage": round(frac, 2),
                "picked": picked_div,
                "headings": [corpus_by_id[c]["heading_path"] for c in picked_div],
            }
            # 变体 D：去冗余 + 邻块回填（两个修法是否叠加）
            expanded_div: List[str] = []
            for cid in picked_div:
                for c2 in expand_ids(cid, corpus_by_id, args.expand, not args.expand_cross_heading):
                    if c2 not in expanded_div:
                        expanded_div.append(c2)
            frac, _ = coverage([corpus_by_id[c]["text"] for c in expanded_div], facts)
            row["stages"]["diverse_expand"] = {
                "coverage": round(frac, 2),
                "chars": sum(len(corpus_by_id[c]["text"]) for c in expanded_div),
                "ids": expanded_div,
            }
        rows.append(row)

    # --- 汇总 ---
    def _mean(values: List[float]) -> float:
        return round(sum(values) / len(values), 3) if values else 0.0

    summary: Dict = {
        "num_chunks": len(corpus),
        "final_k": args.final_k,
        "channel_top_k": args.channel_top_k,
        "by_kind": {},
    }
    for kind in ("overview", "detail"):
        # 只汇总题目口径站得住的那批：把「我的 ground truth 选错」算成系统缺陷，
        # 会把结论带向错误的方向（去修一个不存在的问题）
        subset = [r for r in rows if r["kind"] == kind and r["valid"]]
        if not subset:
            continue
        bucket = {
            "n": len(subset),
            # 这一级「有没有捞到答案片段」（不看名次）
            "in_sparse_pool": sum(1 for r in subset if r["sparse_rank"]),
            "in_dense_pool": sum(1 for r in subset if r["dense_rank"]),
            "in_fused_pool": sum(1 for r in subset if r["fused_rank"]),
            # 答案片段是否已进重排窗口（rerank_n = 生产值 3）
            "in_prod_window": sum(
                1 for r in subset
                if (r["stages"].get("rerank_n3", {}).get("answer_rerank_rank") is not None)
            ),
        }
        for n in rerank_ns:
            key = f"coverage_at_n{n}"
            bucket[key] = _mean([r["stages"][f"rerank_n{n}"]["coverage"] for r in subset if r["stages"]])
        if rerank_scores:
            prod_n = min(rerank_ns)
            bucket["coverage_no_toc"] = _mean(
                [r["stages"][f"notoc_n{prod_n}"]["coverage"] for r in subset if r["stages"]]
            )
            bucket["coverage_expanded"] = _mean(
                [r["stages"]["expand"]["coverage"] for r in subset if r["stages"]]
            )
            bucket["coverage_diverse"] = _mean(
                [r["stages"]["diverse"]["coverage"] for r in subset if r["stages"]]
            )
            bucket["coverage_diverse_expand"] = _mean(
                [r["stages"]["diverse_expand"]["coverage"] for r in subset if r["stages"]]
            )
            chars = [r["stages"]["expand"]["chars"] for r in subset if r["stages"]]
            bucket["avg_expanded_chars"] = int(sum(chars) / len(chars)) if chars else 0
        summary["by_kind"][kind] = bucket

    # --- 打印 ---
    def _fmt(rank: int | None) -> str:
        return "—" if rank is None else str(rank)

    header = f"{'题型':<8} {'问题':<30} {'sparse':>6} {'dense':>5} {'fused':>5}"
    if rerank_scores:
        header += "".join(f"{'cov@' + str(n):>8}" for n in rerank_ns) + f"{'无目录':>8}{'+邻块':>8}{'去冗余':>8}{'去+扩':>8}"
    print("\n" + header)
    print("-" * len(header))
    for r in rows:
        mark = "" if r["valid"] else "（口径存疑）"
        line = f"{r['kind']:<8} {(r['question'] + mark)[:28]:<30} {_fmt(r['sparse_rank']):>6} {_fmt(r['dense_rank']):>5} {_fmt(r['fused_rank']):>5}"
        if rerank_scores:
            line += "".join(f"{r['stages'][f'rerank_n{n}']['coverage']:>8.2f}" for n in rerank_ns)
            line += (f"{r['stages']['notoc_n3']['coverage']:>8.2f}"
                     f"{r['stages']['expand']['coverage']:>8.2f}"
                     f"{r['stages']['diverse']['coverage']:>8.2f}"
                     f"{r['stages']['diverse_expand']['coverage']:>8.2f}")
        print(line)

    print("\n[汇总] 事实点覆盖率（top-%d 拼起来覆盖了几个事实点）" % args.final_k)
    for kind, bucket in summary["by_kind"].items():
        line = (f"  {kind:<9} 召回池命中 sparse {bucket['in_sparse_pool']}/{bucket['n']} "
                f"dense {bucket['in_dense_pool']}/{bucket['n']} "
                f"融合后 {bucket['in_fused_pool']}/{bucket['n']} "
                f"进生产窗口(3) {bucket['in_prod_window']}/{bucket['n']}")
        if rerank_scores:
            line += " | " + " ".join(f"n{n}={bucket[f'coverage_at_n{n}']:.2f}" for n in rerank_ns)
            line += (f" | 无目录={bucket['coverage_no_toc']:.2f}"
                     f" +邻块={bucket['coverage_expanded']:.2f}"
                     f" 去冗余={bucket['coverage_diverse']:.2f}"
                     f" 去冗余+邻块={bucket['coverage_diverse_expand']:.2f}"
                     f"（平均 {bucket['avg_expanded_chars']} 字符）")
        print(line)

    if args.report:
        report = {"rows": rows, "summary": summary, "rerank_seconds": rerank_time}
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[probe] 报告已写入 {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
