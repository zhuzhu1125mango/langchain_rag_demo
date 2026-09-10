"""Wiki 编译层 A/B 检索评估（P2 LLM-Wiki Phase 1，验收标准 §5）。

评估方案（设计文档 llm-wiki-compile.md §5）：
1. 检索指标 A/B：同一问题集分别用「原始 chunk 语料（基线）」与
   「原始 chunk + wiki 编译页混入」跑 BM25 检索，断言混入后
   hit_rate@5 / mrr@5 / recall@5 非退化（wiki 页命中计为其来源文档命中）；
2. 编译事实保留率（WiCER 思路）：从被编译文档抽取原子事实（句子），
   与其编译页分句做 embedding 余弦匹配（阈值与生产探针一致），保留率 ≥ 90% 为上线门槛。

运行方式（需本地 Ollama 可用，默认跳过）：
    cd backend && uv run pytest tests/evaluation/test_wiki_ab.py --run-e2e -v

编译产物不落任何外部系统（compile_pages 纯内存），可重复执行。
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from run_eval import (  # noqa: E402
    DEFAULT_CORPUS,
    DEFAULT_DATASET,
    evaluate,
    load_jsonl,
)

from src.services.wiki_compiler import (  # noqa: E402
    FACT_RETENTION_THRESHOLD,
    WikiCompiler,
    _split_sentences,
)

pytestmark = pytest.mark.e2e

# 事实保留率上线门槛（设计文档 §5.2）
FACT_RETENTION_FLOOR = 0.9
# 参与编译的文档数上限（评估只编译 golden 文档，控制 LLM 调用量）
MAX_DOCS_TO_COMPILE = 4


def _atomic_facts(text: str, min_len: int = 12) -> list:
    """句子级原子事实：按中英文句读切分，过滤过短片段。"""
    parts = re.split(r"[。！？!?\n；;]+", text)
    return [p.strip() for p in parts if len(p.strip()) >= min_len]


def _golden_doc_ids(dataset: list) -> set:
    golden = set()
    for sample in dataset:
        golden.update(sample.get("golden_documents", []))
    return golden


async def _fact_retention(pages_by_doc: dict, corpus: list) -> dict:
    """WiCER 语义事实保留率（口径与生产探针一致，2026-09-10 修正）。

    仅对被编译文档计算：该文档的原子事实 vs 其编译页分句的 embedding
    余弦 max ≥ FACT_RETENTION_THRESHOLD（wiki_compiler 同款阈值）视为保留。
    修正前版本用全语料分母 + 归一化精确子串匹配，对压缩改写型编译页
    结构性不可能达标（实测 0.050），不反映真实编译质量。
    """
    import numpy as np

    from src.services.model_manager import model_manager

    embeddings = model_manager.get_embeddings()
    total = kept = 0
    per_doc = {}
    for doc_id, pages in pages_by_doc.items():
        doc = next((d for d in corpus if d["id"] == doc_id), None)
        facts = _atomic_facts(doc["text"]) if doc else []
        if not facts or not pages:
            per_doc[doc_id] = {"facts": len(facts), "retained": 0}
            continue
        sentences = [s for p in pages for s in _split_sentences(p.content)]
        vectors = await embeddings.aembed_documents(facts + sentences)
        mat = np.asarray(vectors, dtype=float)
        mat = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-12)
        sims = mat[: len(facts)] @ mat[len(facts):].T
        hits = int((sims.max(axis=1) >= FACT_RETENTION_THRESHOLD).sum())
        total += len(facts)
        kept += hits
        per_doc[doc_id] = {"facts": len(facts), "retained": hits}
    rate = kept / total if total else 0.0
    return {"retention": rate, "total_facts": total, "per_doc": per_doc}


def _metrics_with_provenance(result: dict, wiki_source_map: dict, top_k: int = 5) -> dict:
    """§5.1 口径修正（2026-09-10）：wiki 页命中计为其来源文档的命中。

    编译自 golden 文档的 wiki 页排在原文之前不构成检索损害——
    恰是编译层的设计目标（蒸馏后的高密度页）；无关 golden 文档被
    wiki 页挤出 top5 仍会被本口径抓到，真实伤害的检测能力不变。
    """
    details = result["details"]
    n = len(details)
    totals = {"hit_rate": 0.0, "mrr": 0.0, "recall": 0.0}
    for d in details:
        golden = set(d["golden_documents"])
        ranked = [wiki_source_map.get(rid, rid) for rid in d["ranked_top_k"][:top_k]]
        totals["hit_rate"] += 1.0 if golden & set(ranked) else 0.0
        if golden:
            totals["recall"] += len(golden & set(ranked)) / len(golden)
        for rank, rid in enumerate(ranked, start=1):
            if rid in golden:
                totals["mrr"] += 1.0 / rank
                break
    return {k: v / n for k, v in totals.items()} if n else totals


@pytest.mark.e2e
def test_wiki_ab_non_degradation_and_fact_retention():
    """A/B 非退化 + 事实保留率门槛（需本地 Ollama）。"""
    dataset = load_jsonl(DEFAULT_DATASET)
    corpus = load_jsonl(DEFAULT_CORPUS)
    assert dataset and corpus

    import asyncio
    from types import SimpleNamespace

    golden_ids = list(_golden_doc_ids(dataset))[:MAX_DOCS_TO_COMPILE]
    compiler = WikiCompiler()

    async def _compile():
        pages, pages_by_doc = [], {}
        for doc_id in golden_ids:
            doc = next((d for d in corpus if d["id"] == doc_id), None)
            if doc is None:
                continue
            chunks = [SimpleNamespace(page_content=f"{doc['title']}\n{doc['text']}")]
            doc_pages = await compiler.compile_pages(chunks, [])
            pages.extend(doc_pages)
            pages_by_doc[doc_id] = doc_pages
        return pages, pages_by_doc

    wiki_pages, pages_by_doc = asyncio.run(_compile())
    assert wiki_pages, "编译未产出任何页面，检查 Ollama 与编译模型可用性"

    wiki_entries = [
        {"id": f"wiki::{p.title}", "title": p.title, "text": p.content}
        for p in wiki_pages
    ]
    # 测试按文档逐篇编译，wiki 页溯源精确
    wiki_source_map = {
        f"wiki::{p.title}": doc_id
        for doc_id, pages in pages_by_doc.items()
        for p in pages
    }

    baseline = evaluate(dataset, corpus, top_k=5)["metrics"]
    augmented = _metrics_with_provenance(
        evaluate(dataset, corpus + wiki_entries, top_k=5), wiki_source_map
    )

    print("=" * 60)
    print("Wiki A/B 检索评估（BM25 离线代理）")
    print(f"编译页数: {len(wiki_pages)}  页面标题: {[p.title for p in wiki_pages]}")
    for name in ("hit_rate", "mrr", "recall"):
        print(
            f"{name}@5: 基线={baseline[name]:.3f}  "
            f"混入wiki={augmented[name]:.3f}  Δ={augmented[name] - baseline[name]:+.3f}"
        )
    retention = asyncio.run(_fact_retention(pages_by_doc, corpus))
    print(f"事实保留率: {retention['retention']:.3f} "
          f"({retention['total_facts']} 条原子事实)")
    print("=" * 60)

    # 1) 非退化为底线（§5.1）
    for name in ("hit_rate", "mrr", "recall"):
        assert augmented[name] >= baseline[name] - 1e-9, (
            f"Wiki 混入导致检索退化: {name} {baseline[name]:.3f} → {augmented[name]:.3f}"
        )

    # 2) 编译事实保留率门槛（§5.2）
    assert retention["retention"] >= FACT_RETENTION_FLOOR, (
        f"编译事实保留率 {retention['retention']:.3f} < {FACT_RETENTION_FLOOR}，"
        f"需迭代 prompt/更换编译模型（明细: {retention['per_doc']}）"
    )
