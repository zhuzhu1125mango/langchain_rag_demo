"""Wiki 编译层跨文档多跳检索 A/B 评估（roadmap P2-1 启动前置决策工具）。

背景（improvement-roadmap.md §P2-1 约定）：
    P2-1 GraphRAG 启动前需先评估「编译页是否已解决跨文档推理问题」——
    若已解决，P2-1 降级或合并。既有 test_wiki_ab.py 只验证了混入非退化
    （单文档事实类数据集），缺跨文档维度证据；本脚本补上这一块。

设计说明（口径对齐 run_multiquery_ab.py / test_wiki_ab.py）：
- 数据集 wiki_multihop_eval_dataset.jsonl：每个样本的问题需要联合
  ≥2 篇文档的事实才能回答（golden_documents 全部计为命中目标）。
- A 臂（基线）：仅原始 chunk 语料 BM25 检索 Top-K。
- B 臂（混入 wiki）：语料 + 全量文档的 LLM 编译页混入后检索 Top-K；
  wiki 页命中按溯源计为其来源文档命中（口径同 test_wiki_ab §5.1 修正版）。
- C 臂（诊断）：仅 wiki 编译页检索，衡量编译页自身的跨文档召回覆盖，
  用于区分「混入无效」与「编译页本身没编译出相关页」。
- 指标：hit@K / mrr@K / recall@K（多跳集 recall 即"两篇 golden 都进 Top-K"
  的比例，是跨文档推理能否成立的第一道门槛）。

决策规则（供人确认，非自动改开关）：
- B 相对 A 有明确增益（hit 或 recall ≥ +0.05）→ 编译层已部分解决跨文档
  召回缺口，P2-1 GraphRAG 降级/合并；
- B ≈ A 或更差 → 编译页未解决跨文档问题，轻量 GraphRAG 有立项依据。

运行方式（需本地 Ollama 可用，编译全量语料约 12 篇短文档）：
    cd backend && uv run python -u scripts/run_wiki_multihop_ab.py [--report wiki_multihop_ab.json]

说明（AB-CACHE，2026-10-18）：
- `python -u` 保证编译进度实时输出（否则输出重定向时按块缓冲，长时间无日志易误判卡死）。
- 编译页结果按「语料 + 编译模型 + 页数上限/精炼轮数」哈希缓存于
  backend/data/wiki_multihop_ab_cache/（已被 gitignore）；命中即跳过 LLM 编译，
  语料或编译参数变化自动失效。`--no-cache` 强制重编译，`--cache-dir` 改落点。
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = BASE_DIR / "tests" / "evaluation" / "wiki_multihop_eval_dataset.jsonl"
DEFAULT_CORPUS = BASE_DIR / "tests" / "evaluation" / "eval_corpus.jsonl"
DEFAULT_CACHE_DIR = BASE_DIR / "data" / "wiki_multihop_ab_cache"

WIKI_ID_PREFIX = "wiki::"
# 增益判定阈值，与 run_multiquery_ab.recommend 口径一致
GAIN_THRESHOLD = 0.05
# D 臂（导航式）参数：入口页数 / 每入口页沿链接取的邻居页数
NAV_ENTRY_PAGES = 2
NAV_LINKS_PER_PAGE = 3


def wiki_entry_id(doc_id: str, page_title: str) -> str:
    """wiki 检索条目 id：带来源 doc_id 前缀，避免跨文档页标题冲突。"""
    return f"{WIKI_ID_PREFIX}{doc_id}::{page_title}"


def build_link_graph(wiki_entries: List[Dict]) -> Dict[str, List[Dict]]:
    """构建页→邻居页邻接表（离线导航模拟）。

    与生产 `wiki_pages.links` 同源：
    - `WIKI_LINK_METADATA_ENABLED=true`：E-1 L1 元数据建边（正文零改动，
      按「其他页标题出现在本页正文」建边，`derive_links_from_titles`）；
    - 默认：解析正文 `[[Title]]` 语法（旧口径，当前默认编译产物通常为空）。
    """
    from src.config import settings
    from src.services.wiki_compiler import derive_links_from_titles, normalize_title
    from src.services.wiki_lint import _extract_link_titles

    by_title: Dict[str, Dict] = {}
    for entry in wiki_entries:
        by_title.setdefault(normalize_title(entry["title"]), entry)
    metadata_edges = settings.wiki_compile.WIKI_LINK_METADATA_ENABLED
    title_map = {normalize_title(e["title"]): e["title"] for e in wiki_entries}
    graph: Dict[str, List[Dict]] = {}
    for entry in wiki_entries:
        neighbors: List[Dict] = []
        seen_titles = {normalize_title(entry["title"])}
        if metadata_edges:
            link_titles = derive_links_from_titles(entry["text"], entry["title"], title_map)
        else:
            link_titles = _extract_link_titles(entry["text"])
        for link_title in link_titles:
            target = by_title.get(normalize_title(link_title))
            if target is None or target["id"] == entry["id"]:
                continue
            if normalize_title(target["title"]) in seen_titles:
                continue
            seen_titles.add(normalize_title(target["title"]))
            neighbors.append(target)
        graph[entry["id"]] = neighbors
    return graph


def wiki_nav_ranked(bm25, wiki_entries: List[Dict], graph: Dict[str, List[Dict]], question: str, top_k: int) -> List[str]:
    """D 臂：入口检索（仅 wiki 页）→ 顺 links 一跳取邻居页（离线模拟 Agent 导航）。

    入口页在前、邻居页按访问顺序追加，截断 top_k——对应生产中
    "wiki 页做入口定位 + 链接扩展补桥"的机制。
    """
    by_id = {e["id"]: e for e in wiki_entries}
    full_rank = bm25(question, len(wiki_entries))
    ranked: List[str] = []
    seen = set()
    for pid in full_rank[:NAV_ENTRY_PAGES]:
        if pid in seen:
            continue
        seen.add(pid)
        ranked.append(pid)
        for neighbor in graph.get(pid, [])[:NAV_LINKS_PER_PAGE]:
            if neighbor["id"] in seen:
                continue
            seen.add(neighbor["id"])
            ranked.append(neighbor["id"])
    return ranked[:top_k]


def d_recommend(a: Dict, b: Dict, d: Dict) -> str:
    """D 臂（导航式）判定：D ≥ 基线 且 D > 混入 → 导航式修复成立。"""
    if d["recall"] >= a["recall"] - 1e-9 and d["recall"] > b["recall"] + 1e-9:
        return (
            f"导航式（入口+链接）recall@5={d['recall']:.3f} ≥ 基线 {a['recall']:.3f} 且优于混入 {b['recall']:.3f}："
            "跨文档缺口由导航修复，阶段一（导航式 wiki + 链接扩展默认开）立项依据成立。"
        )
    if d["recall"] >= a["recall"] - 1e-9:
        return (
            f"导航式 recall@5={d['recall']:.3f} 与基线持平（{a['recall']:.3f}）、不低于混入（{b['recall']:.3f}）："
            "导航无损害但增量有限，结合基线近天花板，维持保守上线（D1/D2 灰度观察）。"
        )
    return (
        f"导航式 recall@5={d['recall']:.3f} 低于基线 {a['recall']:.3f}：设计证伪，"
        "回退 WIKI_LINK_EXPANSION 默认值并重新评审（见 wiki-navigable-workspace.md §10）。"
    )


async def compile_wiki_pages(corpus: List[Dict]) -> tuple:
    """对全量语料逐篇编译 wiki 页，返回 (检索条目列表, 页→来源文档溯源表)。

    与生产 compile_document 对齐：后编译的文档把先前页面作为 existing_pages
    传入，使编译器能生成跨文档 [[互链]]（链接图是导航式检索的前提）。
    """
    from src.services.wiki_compiler import ExistingPage, WikiCompiler
    from src.services.wiki_lint import _extract_link_titles

    compiler = WikiCompiler()
    entries: List[Dict] = []
    provenance: Dict[str, str] = {}
    existing: List[ExistingPage] = []
    total = len(corpus)
    for i, doc in enumerate(corpus, 1):
        print(f"[wiki-mh-ab] 编译 {i}/{total}：{doc['id']}", flush=True)
        chunks = [SimpleNamespace(page_content=f"{doc['title']}\n{doc['text']}")]
        doc_pages = await compiler.compile_pages(chunks, existing)
        existing.extend(
            ExistingPage(
                page_id="", title=p.title, page_type=p.page_type, content=p.content
            )
            for p in doc_pages
        )
        for page in doc_pages:
            rid = wiki_entry_id(doc["id"], page.title)
            entries.append({"id": rid, "title": page.title, "text": page.content})
            provenance[rid] = doc["id"]
    from src.config import settings
    from src.services.wiki_compiler import derive_links_from_titles, normalize_title
    from src.services.wiki_lint import _extract_link_titles

    if settings.wiki_compile.WIKI_LINK_METADATA_ENABLED:
        title_map = {normalize_title(e["title"]): e["title"] for e in entries}
        n_links = sum(
            len(derive_links_from_titles(e["text"], e["title"], title_map)) for e in entries
        )
        label = "元数据图边（L1，正文零改动）"
    else:
        n_links = sum(len(_extract_link_titles(e["text"])) for e in entries)
        label = "[[互链]]"
    print(f"[wiki-mh-ab] 链接图：{n_links} 条 {label}（0 条说明未建桥）", flush=True)
    return entries, provenance


def compile_cache_key(corpus: List[Dict]) -> str:
    """编译结果缓存键：语料 + 编译模型 + 页数上限/精炼轮数（任一变则失效）。

    AB-CACHE（2026-10-18）：编译是 LLM 逐篇调用（思考模型下全量语料耗时可达小时级），
    缓存命中即跳过重编译，使 E1V 等复跑不必每次盲等。
    """
    import hashlib

    from src.config import settings

    payload = {
        "version": 1,
        "corpus": [
            {"id": d["id"], "title": d["title"], "text": d["text"]} for d in corpus
        ],
        "compile_model": settings.wiki_compile.WIKI_COMPILE_MODEL
        or settings.model.OLLAMA_MODEL_NAME,
        "max_pages_per_doc": settings.wiki_compile.WIKI_COMPILE_MAX_PAGES_PER_DOC,
        "refinement_iterations": settings.wiki_compile.WIKI_COMPILE_REFINEMENT_ITERATIONS,
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


async def compile_wiki_pages_cached(
    corpus: List[Dict], cache_dir: Path, use_cache: bool = True
) -> tuple:
    """带磁盘缓存的编译包装：命中则直读，未命中则编译并回写（AB-CACHE）。"""
    key = compile_cache_key(corpus)
    cache_file = cache_dir / f"wiki_pages_{key}.json"
    if use_cache and cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            entries, provenance = data["entries"], data["provenance"]
            print(f"[wiki-mh-ab] 命中编译缓存（{len(entries)} 页）：{cache_file}", flush=True)
            return entries, provenance
        except Exception as e:  # 缓存损坏则回退重编译，不阻断
            print(f"[wiki-mh-ab] 缓存读取失败，回退重编译：{e}", flush=True)

    entries, provenance = await compile_wiki_pages(corpus)
    if entries:
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(
                json.dumps({"entries": entries, "provenance": provenance}, ensure_ascii=False),
                encoding="utf-8",
            )
            print(f"[wiki-mh-ab] 编译结果已缓存：{cache_file}", flush=True)
        except Exception as e:  # 缓存写入失败不影响本次评估
            print(f"[wiki-mh-ab] 缓存写入失败（忽略）：{e}", flush=True)
    return entries, provenance


def metrics_with_provenance(details: List[Dict], provenance: Dict[str, str], top_k: int = 5) -> Dict:
    """wiki 页命中按溯源计为来源文档命中的指标口径（同 test_wiki_ab §5.1 修正版）。"""
    n = len(details)
    totals = {"hit_rate": 0.0, "mrr": 0.0, "recall": 0.0}
    for d in details:
        golden = set(d["golden_documents"])
        ranked = [provenance.get(rid, rid) for rid in d["ranked_top_k"][:top_k]]
        totals["hit_rate"] += 1.0 if golden & set(ranked) else 0.0
        if golden:
            totals["recall"] += len(golden & set(ranked)) / len(golden)
        for rank, rid in enumerate(ranked, start=1):
            if rid in golden:
                totals["mrr"] += 1.0 / rank
                break
    return {k: v / n for k, v in totals.items()} if n else totals


def recommend(a: Dict, b: Dict, wiki_only: Dict) -> str:
    """基于 A/B(+C 诊断) 指标给出 P2-1 决策建议（供人确认）。"""
    hit_gain = b["hit_rate"] - a["hit_rate"]
    recall_gain = b["recall"] - a["recall"]
    if hit_gain >= GAIN_THRESHOLD or recall_gain >= GAIN_THRESHOLD:
        return (
            f"混入 wiki 编译页对跨文档问题有明确增益（hit {hit_gain:+.3f} / recall {recall_gain:+.3f}），"
            "编译层已部分解决跨文档召回缺口：P2-1 GraphRAG 建议降级/合并，"
            "优先沿 Wiki 路线增强（覆盖先验/链接扩展调优）。"
        )
    if wiki_only["recall"] < a["recall"] - 1e-9:
        return (
            f"混入无增益（hit {hit_gain:+.3f} / recall {recall_gain:+.3f}），且 wiki 页自身跨文档召回"
            f"（recall@5={wiki_only['recall']:.3f}）低于原始 chunk 基线（{a['recall']:.3f}）："
            "编译页未覆盖跨文档关联信息——若确认跨文档问答是核心场景，轻量 GraphRAG 有立项依据。"
        )
    return (
        f"混入无增益（hit {hit_gain:+.3f} / recall {recall_gain:+.3f}），编译页未带来跨文档召回改善："
        "轻量 GraphRAG 有立项依据（实体链接一跳补充上下文），但需结合线上跨文档问题占比确认投入产出比。"
    )


def w2_recommend(a: Dict, b: Dict) -> str:
    """E-1 W2 退主池判定：现状 B（raw+wiki）vs 退池 A（仅 raw）。

    退池 == 仅 raw：两臂在主检索口径下等价（编译页全退主池后池中只剩原始 chunk），
    故"三臂（现状/退池/仅 raw）"实质为两臂比较；落地开关为
    KB_RETRIEVAL_EXCLUDE_SOURCE_KINDS（"wiki_syn"=现状 / "wiki,wiki_syn"=退池）。
    """
    d_recall = a["recall"] - b["recall"]
    d_hit = a["hit_rate"] - b["hit_rate"]
    if d_recall >= GAIN_THRESHOLD:
        return (
            f"退池（仅 raw，recall@5={a['recall']:.3f}）优于现状（raw+wiki，{b['recall']:.3f}）"
            f"recall {d_recall:+.3f}（hit {d_hit:+.3f}）：编译页混入主池稀释跨文档召回 → "
            "采纳退池（KB_RETRIEVAL_EXCLUDE_SOURCE_KINDS=wiki,wiki_syn）。"
        )
    if d_recall <= -GAIN_THRESHOLD:
        return (
            f"现状（raw+wiki，recall@5={b['recall']:.3f}）优于退池（仅 raw，{a['recall']:.3f}）"
            f"recall {d_recall:+.3f}：编译页对主检索有净增益 → 维持现状（exclude 仅 wiki_syn）。"
        )
    return (
        f"退池与现状近持平（Δrecall={d_recall:+.3f} / Δhit={d_hit:+.3f}）：无明确增益，"
        "维持现状（保守）；开关可随时切换。"
    )


def main() -> int:
    for p in (str(BASE_DIR / "scripts"), str(BASE_DIR)):
        if p not in sys.path:
            sys.path.insert(0, p)

    parser = argparse.ArgumentParser(description="Wiki 跨文档多跳检索 A/B 评估（P2-1 决策工具）")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR,
                        help="编译页缓存目录（默认 backend/data/wiki_multihop_ab_cache）")
    parser.add_argument("--no-cache", action="store_true", help="忽略缓存，强制重编译")
    args = parser.parse_args()

    from run_eval import evaluate, load_jsonl

    dataset = load_jsonl(args.dataset)
    corpus = load_jsonl(args.corpus)
    if not dataset or not corpus:
        print(f"[wiki-mh-ab] 数据集或语料为空（dataset={len(dataset)}, corpus={len(corpus)}）")
        return 2
    bad = [s["question"] for s in dataset if len(s.get("golden_documents", [])) < 2]
    if bad:
        print(f"[wiki-mh-ab] 存在 golden_documents 少于 2 篇的样本（非跨文档）: {bad}")
        return 2

    print(f"[wiki-mh-ab] 编译全量语料 wiki 页（{len(corpus)} 篇，需本地 Ollama；命中缓存则跳过）...", flush=True)
    wiki_entries, provenance = asyncio.run(
        compile_wiki_pages_cached(corpus, args.cache_dir, use_cache=not args.no_cache)
    )
    if not wiki_entries:
        print("[wiki-mh-ab] 编译未产出任何页面，检查 Ollama 与编译模型可用性")
        return 2
    print(f"[wiki-mh-ab] 编译完成：{len(wiki_entries)} 页")

    a_metrics = evaluate(dataset, corpus, top_k=args.top_k)["metrics"]
    b_metrics = metrics_with_provenance(
        evaluate(dataset, corpus + wiki_entries, top_k=args.top_k)["details"], provenance, args.top_k
    )
    c_metrics = metrics_with_provenance(
        evaluate(dataset, wiki_entries, top_k=args.top_k)["details"], provenance, args.top_k
    )

    # D 臂：导航式（入口 + 一跳链接），先构建全量 ranked 明细再走溯源口径
    bm25 = _build_bm25_for_wiki(wiki_entries)
    graph = build_link_graph(wiki_entries)
    d_details = []
    for sample in dataset:
        d_details.append({
            "question": sample["question"],
            "golden_documents": sample["golden_documents"],
            "ranked_top_k": wiki_nav_ranked(
                bm25, wiki_entries, graph, sample["question"], args.top_k
            ),
        })
    d_metrics = metrics_with_provenance(d_details, provenance, args.top_k)

    print("=" * 70)
    print("Wiki 编译层跨文档多跳检索 A/B（BM25 离线代理，P2-1 决策证据）")
    print("=" * 70)
    print(f"多跳问题数={len(dataset)}  wiki页数={len(wiki_entries)}  top_k={args.top_k}")
    print(f"  A(仅原始chunk)      hit@{args.top_k}={a_metrics['hit_rate']:.3f}  mrr@{args.top_k}={a_metrics['mrr']:.3f}  recall@{args.top_k}={a_metrics['recall']:.3f}")
    print(f"  B(原始chunk+wiki)   hit@{args.top_k}={b_metrics['hit_rate']:.3f}  mrr@{args.top_k}={b_metrics['mrr']:.3f}  recall@{args.top_k}={b_metrics['recall']:.3f}")
    print(f"  C(仅wiki页,诊断)    hit@{args.top_k}={c_metrics['hit_rate']:.3f}  mrr@{args.top_k}={c_metrics['mrr']:.3f}  recall@{args.top_k}={c_metrics['recall']:.3f}")
    print(f"  D(导航式,阶段一)    hit@{args.top_k}={d_metrics['hit_rate']:.3f}  mrr@{args.top_k}={d_metrics['mrr']:.3f}  recall@{args.top_k}={d_metrics['recall']:.3f}")
    print(f"  结论：{recommend(a_metrics, b_metrics, c_metrics)}")
    print(f"  D 臂判定：{d_recommend(a_metrics, b_metrics, d_metrics)}")
    print(f"  W2 退主池判定：{w2_recommend(a_metrics, b_metrics)}")

    if args.report:
        payload = {
            "num_questions": len(dataset),
            "top_k": args.top_k,
            "wiki_pages": len(wiki_entries),
            "arm_A_raw": a_metrics,
            "arm_B_mixed": b_metrics,
            "arm_C_wiki_only": c_metrics,
            "arm_D_navigated": d_metrics,
            "w2_arms": {
                "现状_raw_plus_wiki": b_metrics,
                "退池_raw_only": a_metrics,
                "仅raw_等价退池": a_metrics,
            },
            "recommendation": recommend(a_metrics, b_metrics, c_metrics),
            "d_recommendation": d_recommend(a_metrics, b_metrics, d_metrics),
            "w2_recommendation": w2_recommend(a_metrics, b_metrics),
        }
        args.report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[wiki-mh-ab] 报告已写入: {args.report}")
    return 0


def _build_bm25_for_wiki(wiki_entries: List[Dict]):
    """构造作用于 wiki 页条目的 BM25 排序函数（复用 run_eval 的语料口径）。"""
    import run_eval

    def bm25(question: str, top_k: int) -> List[str]:
        ranked = run_eval.bm25_rank(wiki_entries, question, top_k=top_k)
        return ranked

    return bm25


if __name__ == "__main__":
    sys.exit(main())
