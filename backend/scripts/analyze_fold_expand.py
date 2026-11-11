"""「融合后折叠」与「邻块回填」的组合验证：2×2 覆盖 + 上下文成本 + 预算扫描。

## 为什么单独算一次

两份生产探针报告（`--fold-table` 开/关）里都已经带了 `to_expand_coverage`，
但那个数只说「覆盖率会变成多少」，不回答两个更要紧的问题：

1. **回填的代价**：上下文从几个字符涨到几个字符。回填把 3 块变成最多 8 块，
   无预算时字符数涨近 3 倍，而覆盖率只多几个点 —— 「值不值」取决于付多少；
2. **两者是否互补**：折叠改变「哪 3 块进上下文」，回填在「这 3 块周围」补邻居。
   如果折叠后回填的增量明显大于折叠前，说明两者作用在不同位置，可以叠加。

## 口径

- 覆盖率：按 `probe_retrieval_stages.QUESTIONS` 的事实串重新计算（不直接读 JSON 的
  `to_expand_coverage`），这样预算扫描才可能——JSON 里只存了无预算的结果；
- 字符数：从生产 Milvus 读每个块的 `content` 真实长度（JSON 里的 preview 被截断了，
  不能用它算成本）；
- 邻块定义与探针一致：同 `document_id`、`chunk_index ± 1`。

只读，不写 Milvus、不改配置。

    docker exec backend-prod python scripts/analyze_fold_expand.py \
        --off /app/data/probe-prod-foldoff-20261111.json \
        --on  /app/data/probe-prod-foldon-20261111.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR / "scripts"))

from src.config import settings  # noqa: E402
from src.services.milvus_service import MilvusService  # noqa: E402

# 复用同一批题与同一个覆盖率定义——两套口径算出来的数不可比
from probe_retrieval_stages import QUESTIONS, coverage  # noqa: E402

FACTS: Dict[str, List[str]] = {item["q"]: item["facts"] for item in QUESTIONS}


def expand_with_neighbors(ids: Sequence[str], index: Dict[Tuple[str, int], str],
                          by_id: Dict[str, Dict], cap_chars: int | None = None) -> List[str]:
    """同文档 chunk_index ±1 回填，顺序保持「先原地、再邻块」。

    `cap_chars` 为总字符上限（含原地块）：这是**成本控制**的关键旋钮——
    无上限时 3 块会涨到 8 块、上下文字符涨近 3 倍，而覆盖率只多几个点。
    """
    out: List[str] = list(ids)
    seen = set(out)
    used = sum(len(by_id[c]["content"] or "") for c in out if c in by_id)
    for cid in ids:
        row = by_id.get(cid) or {}
        doc, idx = row.get("document_id"), row.get("chunk_index")
        if doc is None or idx is None:
            continue
        for delta in (-1, 1):
            nb = index.get((doc, idx + delta))
            if not nb or nb in seen:
                continue
            if cap_chars is not None:
                grow = len(by_id[nb]["content"] or "")
                if used + grow > cap_chars:
                    continue
                used += grow
            out.append(nb)
            seen.add(nb)
    return out


def evaluate(rows: List[Dict], by_id: Dict[str, Dict], index: Dict[Tuple[str, int], str],
             cap_chars: int | None, stage: str = "rerank_n3") -> Dict[str, Dict]:
    """按题型汇总：覆盖率、上下文字符、块数。`cap_chars=None` 表示不回填。"""
    out: Dict[str, Dict] = {}
    for kind in ("overview", "detail"):
        sub = [r for r in rows if r["kind"] == kind]
        if not sub:
            continue
        covs, chars, nchunks = [], [], []
        for r in sub:
            picked = [p["id"] for p in r["stages"][stage]["picked"]]
            ids = picked if cap_chars is None else expand_with_neighbors(picked, index, by_id, cap_chars)
            texts = [by_id[c]["content"] for c in ids if c in by_id]
            covs.append(coverage(texts, FACTS[r["question"]])[0])
            chars.append(sum(len(t or "") for t in texts))
            nchunks.append(len(ids))
        out[kind] = {
            "n": len(sub),
            "coverage": round(statistics.mean(covs), 3),
            "chars": int(statistics.mean(chars)),
            "chunks": round(statistics.mean(nchunks), 1),
        }
    return out


def per_question(rows: List[Dict], by_id: Dict[str, Dict], index: Dict[Tuple[str, int], str],
                 cap_chars: int | None = None, stage: str = "rerank_n3") -> Dict[str, Dict]:
    out = {}
    for r in rows:
        picked = [p["id"] for p in r["stages"][stage]["picked"]]
        ids = picked if cap_chars is None else expand_with_neighbors(picked, index, by_id, cap_chars)
        texts = [by_id[c]["content"] for c in ids if c in by_id]
        frac, hits = coverage(texts, FACTS[r["question"]])
        out[r["question"]] = {
            "kind": r["kind"],
            "coverage": round(frac, 3),
            "hits": hits,
            "chars": sum(len(t or "") for t in texts),
            "chunks": len(ids),
        }
    return out


async def main_async(args) -> int:
    svc = await MilvusService.get_instance()
    rows = await svc.client.query(
        collection_name=settings.milvus.MILVUS_COLLECTION_NAME,
        filter="id != ''",
        output_fields=["id", "document_id", "chunk_index", "content"],
        limit=16384,
    )
    by_id = {r["id"]: r for r in rows}
    index = {(r["document_id"], r["chunk_index"]): r["id"] for r in rows}
    print(f"[fold+expand] 语料 {len(by_id)} 块")

    off = json.load(open(args.off, encoding="utf-8"))["rows"]
    on = json.load(open(args.on, encoding="utf-8"))["rows"]

    print(f"\n{'配置':<22}{'概览':>7}{'细节':>7}{'概览字符':>9}{'细节字符':>9}{'块数':>6}")
    print("-" * 62)
    grid = {}
    for fold_label, rows_ in (("不折叠", off), ("折叠", on)):
        for cap_label, cap in (("不回填", None), ("回填(无预算)", 0)):
            key = f"{fold_label}/{cap_label}"
            s = evaluate(rows_, by_id, index, None if cap is None else 10 ** 9)
            grid[key] = s
            print(f"{key:<24}{s['overview']['coverage']:>7.3f}{s['detail']['coverage']:>7.3f}"
                  f"{s['overview']['chars']:>9}{s['detail']['chars']:>9}{s['overview']['chunks']:>6.1f}")

    print(f"\n回填预算扫描（概览覆盖率 / 概览字符 / 块数；基线是不回填）")
    print(f"{'预算':<10}{'不折叠':>18}{'折叠':>18}")
    print("-" * 48)
    sweep = {}
    for cap in [900, 1200, 1600, 2000, 2600, 10 ** 9]:
        row = {}
        for label, rows_ in (("off", off), ("on", on)):
            s = evaluate(rows_, by_id, index, cap)["overview"]
            row[label] = s
        sweep[cap] = row
        cap_label = "无上限" if cap > 10 ** 8 else str(cap)
        print(f"{cap_label:<10}"
              f"{row['off']['coverage']:>7.3f}/{row['off']['chars']:>5}/{row['off']['chunks']:>4.1f}"
              f"{row['on']['coverage']:>9.3f}/{row['on']['chars']:>5}/{row['on']['chunks']:>4.1f}")

    # 逐题：回填带来的覆盖增量（回填前 → 回填后），折叠前后各一列
    q_off_base = per_question(off, by_id, index, None)
    q_off_exp = per_question(off, by_id, index, 10 ** 9)
    q_on_base = per_question(on, by_id, index, None)
    q_on_exp = per_question(on, by_id, index, 10 ** 9)
    print("\n回填带来的覆盖增量（回填前 → 后），只列有变化的题：")
    print(f"{'题':34}{'不折叠':>16}{'折叠':>16}")
    for q in q_off_base:
        a0, a1 = q_off_base[q]["coverage"], q_off_exp[q]["coverage"]
        b0, b1 = q_on_base[q]["coverage"], q_on_exp[q]["coverage"]
        if abs(a1 - a0) > 1e-9 or abs(b1 - b0) > 1e-9:
            print(f"{q[:32]:34}{a0:>7.2f}→{a1:<7.2f}{b0:>7.2f}→{b1:<7.2f}")

    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(
            json.dumps({"grid": grid, "sweep": {str(k): v for k, v in sweep.items()},
                        "off": q_off_exp, "on": q_on_exp}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n报告已写入：{args.report}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="折叠 + 邻块回填的 2×2 验证与预算扫描（只读）")
    parser.add_argument("--off", required=True, help="折叠关闭时的探针报告")
    parser.add_argument("--on", required=True, help="折叠开启时的探针报告")
    parser.add_argument("--report", default="", help="汇总结果写 JSON")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
