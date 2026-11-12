"""答案侧诊断：检索到的事实，有多少真的进了答案？

## 为什么需要它（本轮之前从没测过这一层）

今天的全部工作都在检索侧。`stage-probe` / `prod-probe` 报的「概览 0.681」是
**上下文**的事实点覆盖率——它只说明「该有的被放进了上下文」，不说明
「模型把它们写进了答案」。这两件事之间的差额，此前没有任何一个指标在看。

2026-11-12 的第一个样本就露出了问题：

    问：监控哪些指标、有哪些告警？
    上下文：指标表 + 告警表（覆盖率 0.50，告警那条事实在里面）
    答案：只列了 3 个指标，一个字没提告警
    answer_type：knowledge_base（系统认为「我答上了」）

用户拿到的是**残缺但自信**的答案，而检索指标和 `answer_type` 都没报警。

## 三个客观指标（不需要 LLM 判分）

1. `ctx_cov` —— 上下文里命中了几个事实点（沿用既有口径，可与历史数据比）
2. `ans_cov` —— **答案文本**里命中了几个事实点
3. `gap = ctx_cov - ans_cov` —— 检索到了却没写出来，这是生成侧的信息丢失

外加两个分类：
- `refused` —— 答案含「未找到 / 资料中没有 / 无法回答」等表述（诚实的不知道）
- `confident_miss` —— `ctx_cov >= 0.5` 但 `ans_cov` 明显更低且没有拒答（自信地漏答）

第 4 项是最要紧的：它把「检索好坏」和「用户感受」之间的鸿沟变成了一个可数的数字。

## 用法（容器内跑，绕开 HTTP 鉴权）

    docker cp backend/data/eval_questions.json backend-prod:/app/data/eval_questions.json
    docker cp backend/scripts/probe_answer_quality.py backend-prod:/app/scripts/
    docker exec backend-prod python scripts/probe_answer_quality.py \
        --kb b3bad7b2-998c-47ec-8398-f70732081d3a --report /app/data/answer-quality-20261112.json

成本：每题约 20~30s（qwen3:4b，含思考），24 题约 10 分钟。不进 CI。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from src.services.rag_chain import RAGChain  # noqa: E402

# 拒答/说明性表述：出现这些说明模型意识到「资料不够」，而不是硬答
REFUSAL_MARKERS = (
    "未找到", "没有找到", "没有相关", "无相关", "资料中没有", "资料中未",
    "未提及", "没有提及", "无法回答", "无法确定", "知识库中没有", "知识库中未",
    "暂无", "不包含", "没有提供",
)

# 上下文与答案差的判定阈值：差 1 个事实点（48 点 / 24 题 → 每题平均 2 点）
GAP_WARN = 0.25


def coverage(texts: Sequence[str], facts: Sequence[str]):
    """事实点覆盖率：facts 中有几个出现在 texts 的任意一段里。"""
    blob = "\n".join(t for t in texts if t)
    hits = [f for f in facts if f in blob]
    return (len(hits) / len(facts) if facts else 0.0), hits


def extract_context(state: Any) -> List[str]:
    """从 pipeline 终态里取「实际喂给模型的上下文」。

    字段名不硬编码：不同版本分别用 source_texts（引用原文）/ docs（Document 列表）。
    取不到就返回空，并在输出里标出来——宁可显示「取不到」，也不能默认成「上下文为空」
    从而把 gap 算成 0（那会把最严重的问题藏起来）。
    """
    for attr in ("source_texts", "context_texts"):
        val = getattr(state, attr, None)
        if val:
            return [str(t) for t in val]
    docs = getattr(state, "docs", None)
    if docs:
        out = []
        for d in docs:
            text = getattr(d, "page_content", None)
            out.append(text if text is not None else str(d))
        return out
    return []


async def run_one(chain, item: Dict, kb_id: str, session: str) -> Dict:
    q, facts = item["q"], item["facts"]
    parts: List[str] = []
    answer_type = None
    sources = None
    state = None

    t0 = time.perf_counter()
    try:
        async for ev in chain._pipeline(
            q, kb_ids=[kb_id], user_id=None, session_id=session
        ):
            kind = ev[0]
            if kind == "chunk":
                parts.append(ev[1] or "")
                if len(ev) > 4 and ev[4]:
                    answer_type = ev[4]
                if len(ev) > 2 and ev[2] is not None:
                    sources = ev[2]
            elif kind == "final":
                state = ev[1]
    except Exception as exc:  # noqa: BLE001 —— 单题失败不该终止整轮
        return {
            "question": q, "kind": item["kind"], "error": f"{type(exc).__name__}: {exc}",
            "elapsed_s": round(time.perf_counter() - t0, 1),
        }
    elapsed = time.perf_counter() - t0

    answer = "".join(parts)
    ctx_texts = extract_context(state) if state is not None else []
    ctx_cov, ctx_hits = coverage(ctx_texts, facts)
    ans_cov, ans_hits = coverage([answer], facts)
    refused = any(m in answer for m in REFUSAL_MARKERS)
    atype = answer_type or getattr(state, "answer_type", None) or ""

    return {
        "question": q,
        "kind": item["kind"],
        "facts": len(facts),
        "ctx_coverage": round(ctx_cov, 3),
        "ans_coverage": round(ans_cov, 3),
        "gap": round(ctx_cov - ans_cov, 3),
        "ctx_hits": ctx_hits,
        "ans_hits": ans_hits,
        "missing_in_answer": [f for f in ctx_hits if f not in ans_hits],
        "answer_type": atype,
        "refused": refused,
        # 「自信地漏答」：有足够上下文、答案却明显更少、而且没有一句「资料里没写」
        "confident_miss": bool(ctx_cov >= 0.5 and (ctx_cov - ans_cov) >= GAP_WARN and not refused),
        "ctx_chars": sum(len(t) for t in ctx_texts),
        "answer_chars": len(answer),
        "sources": len(sources) if sources else 0,
        "elapsed_s": round(elapsed, 1),
        "answer_preview": answer[:200].replace("\n", " "),
    }


async def main_async(args) -> int:
    items = json.load(open(args.questions, encoding="utf-8"))
    if args.limit:
        items = items[: args.limit]
    print(f"[answer-probe] {len(items)} 题 / kb={args.kb}")

    # 语义缓存会让重复评测直接命中旧答案：检索不走、上下文为空，gap 会被算成 0
    # （或者反过来，答案来自旧上下文而指标看着很好）。评测必须在干净缓存下跑。
    if args.flush_cache:
        try:
            from src.services.semantic_cache_service import SemanticCacheService

            await SemanticCacheService().invalidate_kb(args.kb)
            print("[answer-probe] 已清除该知识库的语义缓存")
        except Exception as exc:  # noqa: BLE001 —— 清缓存失败不该终止评测
            print(f"[answer-probe] 清缓存失败（继续跑，结果可能混入缓存命中）: {exc}")

    chain = await RAGChain.get_instance()
    rows: List[Dict] = []
    t0 = time.perf_counter()
    for i, item in enumerate(items, 1):
        row = await run_one(chain, item, args.kb, args.session)
        rows.append(row)
        if "error" in row:
            print(f"  [{i:>2}/{len(items)}] ERROR {row['error'][:70]}")
        else:
            flag = " ⚠自信漏答" if row["confident_miss"] else (" 拒答" if row["refused"] else "")
            print(f"  [{i:>2}/{len(items)}] ctx={row['ctx_coverage']:.2f} ans={row['ans_coverage']:.2f} "
                  f"gap={row['gap']:+.2f} {row['answer_type'][:14]:<14}{flag} | {row['question'][:26]}")

    ok = [r for r in rows if "error" not in r]
    print(f"\n[汇总] 有效 {len(ok)}/{len(rows)} 题，总耗时 {time.perf_counter()-t0:.0f}s")
    if ok:
        for kind in ("overview", "detail"):
            sub = [r for r in ok if r["kind"] == kind]
            if not sub:
                continue
            print(f"  {kind:<9} n={len(sub):<3} "
                  f"上下文 {statistics.mean(r['ctx_coverage'] for r in sub):.3f} "
                  f"答案 {statistics.mean(r['ans_coverage'] for r in sub):.3f} "
                  f"gap {statistics.mean(r['gap'] for r in sub):+.3f}")
        print(f"  拒答（诚实说不知道）      {sum(1 for r in ok if r['refused'])}/{len(ok)}")
        print(f"  **自信漏答**（有料却没写） {sum(1 for r in ok if r['confident_miss'])}/{len(ok)}")
        # 「上下文为空却仍以知识库作答」= 最典型的自信错答
        ghost = [r for r in ok if r["ctx_coverage"] == 0 and r["answer_type"].startswith("knowledge")]
        print(f"  上下文零覆盖仍标 knowledge_base {len(ghost)}/{len(ok)}")
        for r in ghost:
            print(f"      - {r['question'][:30]} | ans_cov={r['ans_coverage']:.2f}")

    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(
            {"rows": rows, "kb": args.kb, "session": args.session}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"\n报告已写入：{args.report}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="答案侧诊断：上下文 vs 答案的事实覆盖")
    parser.add_argument("--questions", default="/app/data/eval_questions.json")
    parser.add_argument("--kb", required=True, help="知识库 ID（项目文档库）")
    parser.add_argument("--session", default="eval-answer-20261112", help="trace 归属标记，便于区分评测流量")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 题（冒烟用）")
    parser.add_argument("--flush-cache", action="store_true", help="跑前清除该 KB 的语义缓存（重跑必须加）")
    parser.add_argument("--report", default="")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
