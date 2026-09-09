"""Wiki 编译层 A/B 检索评估（P2 LLM-Wiki Phase 1，验收标准 §5）。

评估方案（设计文档 llm-wiki-compile.md §5）：
1. 检索指标 A/B：同一问题集分别用「原始 chunk 语料（基线）」与
   「原始 chunk + wiki 编译页混入」跑 BM25 检索，断言混入后
   hit_rate@5 / mrr@5 / recall@5 非退化；
2. 编译事实保留率（WiCER 思路）：从被编译文档抽取原子事实（句子），
   检查编译页是否保留，保留率 ≥ 90% 为上线门槛。

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

from src.services.wiki_compiler import WikiCompiler  # noqa: E402

pytestmark = pytest.mark.e2e

# 事实保留率上线门槛（设计文档 §5.2）
FACT_RETENTION_FLOOR = 0.9
# 参与编译的文档数上限（评估只编译 golden 文档，控制 LLM 调用量）
MAX_DOCS_TO_COMPILE = 4


def _normalize(text: str) -> str:
    """事实匹配归一化：去空白与中英文标点，转小写。"""
    return re.sub(r"[\s，。；：、！？!？;:,.\-—()（）\"'「」《》]+", "", (text or "")).lower()


def _atomic_facts(text: str, min_len: int = 12) -> list:
    """句子级原子事实：按中英文句读切分，过滤过短片段。"""
    parts = re.split(r"[。！？!?\n；;]+", text)
    return [p.strip() for p in parts if len(p.strip()) >= min_len]


def _golden_doc_ids(dataset: list) -> set:
    golden = set()
    for sample in dataset:
        golden.update(sample.get("golden_documents", []))
    return golden


def _fact_retention(corpus: list, wiki_pages: list) -> dict:
    """计算编译事实保留率：源文档原子事实在编译页中的覆盖率。"""
    wiki_text = _normalize("\n".join(p.content for p in wiki_pages))
    total = kept = 0
    per_doc = {}
    for doc in corpus:
        facts = _atomic_facts(doc["text"])
        if not facts:
            continue
        hits = sum(1 for f in facts if _normalize(f) in wiki_text)
        total += len(facts)
        kept += hits
        per_doc[doc["id"]] = {"facts": len(facts), "retained": hits}
    rate = kept / total if total else 0.0
    return {"retention": rate, "total_facts": total, "per_doc": per_doc}


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
        pages = []
        for doc_id in golden_ids:
            doc = next((d for d in corpus if d["id"] == doc_id), None)
            if doc is None:
                continue
            chunks = [SimpleNamespace(page_content=f"{doc['title']}\n{doc['text']}")]
            pages.extend(await compiler.compile_pages(chunks, []))
        return pages

    wiki_pages = asyncio.run(_compile())
    assert wiki_pages, "编译未产出任何页面，检查 Ollama 与编译模型可用性"

    wiki_entries = [
        {"id": f"wiki::{p.title}", "title": p.title, "text": p.content}
        for p in wiki_pages
    ]

    baseline = evaluate(dataset, corpus, top_k=5)["metrics"]
    augmented = evaluate(dataset, corpus + wiki_entries, top_k=5)["metrics"]

    print("=" * 60)
    print("Wiki A/B 检索评估（BM25 离线代理）")
    print(f"编译页数: {len(wiki_pages)}  页面标题: {[p.title for p in wiki_pages]}")
    for name in ("hit_rate", "mrr", "recall"):
        print(
            f"{name}@5: 基线={baseline[name]:.3f}  "
            f"混入wiki={augmented[name]:.3f}  Δ={augmented[name] - baseline[name]:+.3f}"
        )
    retention = _fact_retention(corpus, wiki_pages)
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
