"""答案侧覆盖率的复核：区分「真漏写」与「换了个说法」。

## 为什么要复核

`probe_answer_quality` 用的是**严格子串匹配**：事实串必须逐字出现在答案里。
这在第一轮没问题（它抓出的是空格 bug —— 那种漏是真的没写），但它会误判三类：

    事实串 "alembic downgrade"  vs 答案 "…alembic 的 downgrade 指令…"   → 判漏，实为有
    事实串 "不可打乱"           vs 答案 "…必须严格按照指定的顺序执行…"    → 判漏，实为换说法
    事实串 "恢复顺序"           vs 答案 "…恢复的顺序不能打乱…"           → 判漏，实为多一个"的"

2026-11-12 打开折叠+回填后，概览答案覆盖率只涨 2.1pp，注解却有 4 题从 0.5 掉到 0.0。
逐题读答案才发现：4 题里有 2 题是上面这类**措辞差异**，1 题只差一个文件名，
1 题是真的答错了（把「reload 生效」答成「restart 生效」）。

所以这个脚本给出两套口径：

- `strict` —— 与 `probe_answer_quality` 完全一致（可跨历史版本比）
- `loose`  —— 去空白 + 支持 `A|B` 备选 + 拉丁词按 token 全含

两者之差就是**指标自身的假漏写**。先在假漏写上做优化，等于给测量误差写代码。

## 用法

    docker exec backend-prod python scripts/analyze_answer_gap.py \
        --base /app/data/answer-quality-base-20261112.json \
        --variant /app/data/answer-quality-foldexpand-20261112.json \
        --questions /app/data/eval_questions.json

两份报告都必须带 `--save-answers` 跑出来（否则只有 200 字预览，无法复核）。
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from typing import Dict, List, Sequence, Tuple

# 拉丁词/数字的 token：中英混排的事实串按它拆（"alembic downgrade" → 两个 token）
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]*|\d+(?:\.\d+)?")


def _norm(text: str) -> str:
    """去掉所有空白并小写：中文的换行、英文的缩进都不该影响判定。"""
    return re.sub(r"\s+", "", text or "").lower()


def fact_hit_strict(fact: str, blob: str) -> bool:
    """逐字命中（与 probe_answer_quality 保持一致）；`A|B` 视为备选。"""
    return any(alt and alt in blob for alt in fact.split("|"))


def fact_hit_loose(fact: str, blob: str) -> bool:
    """宽松命中：去空白子串，或拉丁 token 全部出现。

    token 规则只对**含拉丁 token 的事实串**生效。纯中文短语（如「不可打乱」）
    只走子串判定 —— 否则单字级别的要求会让所有题都「命中」，指标就失去意义。
    """
    nb = _norm(blob)
    for alt in fact.split("|"):
        alt = alt.strip()
        if not alt:
            continue
        if _norm(alt) in nb:
            return True
        tokens = _TOKEN_RE.findall(alt)
        if tokens and all(t.lower() in nb for t in tokens):
            return True
    return False


def score(answer: str, facts: Sequence[str], loose: bool) -> Tuple[float, List[str], List[str]]:
    hit = fact_hit_loose if loose else fact_hit_strict
    hits = [f for f in facts if hit(f, answer)]
    miss = [f for f in facts if f not in hits]
    return (len(hits) / len(facts) if facts else 0.0), hits, miss


def load(path: str) -> Dict[str, Dict]:
    rows = json.load(open(path, encoding="utf-8"))["rows"]
    out = {}
    for r in rows:
        if "answer" not in r:
            raise SystemExit(
                f"{path} 里没有答案全文：请带 --save-answers 重跑，否则无法复核措辞差异"
            )
        out[r["question"]] = r
    return out


def pooled(arm: Dict[str, Dict], facts: Dict[str, List[str]], kind: str, loose: bool) -> Tuple[int, int]:
    """池化命中数：(命中事实点总数, 事实点总数)。

    为什么不按题平均：24 题按题型拆成 12 题后，「一题一个事实点」= 8.3pp，
    这比要判断的差异还大，指标分辨不出信号。池化后按事实点计数（本评测集
    概览 30 点、细节 18 点），一个点 = 3.3pp / 5.6pp——同样数量的题，
    分辨率提升 2~3 倍，且不需要重跑。

    代价：它会偏向事实点多的题（一道 6 点的题权重是 2 点题的 3 倍）。
    对「上下文里有的东西有没有被写出来」这个问题，这正是想要的权重。
    """
    hit = fact_hit_loose if loose else fact_hit_strict
    total = got = 0
    for q, r in arm.items():
        if r["kind"] != kind:
            continue
        fs = facts.get(q, [])
        total += len(fs)
        got += sum(1 for f in fs if hit(f, r["answer"]))
    return got, total


def main() -> int:
    ap = argparse.ArgumentParser(description="答案覆盖率复核：严格 vs 宽松口径")
    ap.add_argument("--base", default="", help="基线报告（带答案全文）")
    ap.add_argument("--variant", default="", help="对照报告（带答案全文）")
    ap.add_argument("--questions", default="/app/data/eval_questions.json")
    ap.add_argument("--json-out", default="", help="逐题明细写 JSON")
    ap.add_argument("--arms", nargs="*", default=[],
                    help="多臂对比：name=path ...（池化口径，分辨率更高）")
    args = ap.parse_args()

    facts = {it["q"]: it["facts"] for it in json.load(open(args.questions, encoding="utf-8"))}

    if args.arms:
        rows = []
        for spec in args.arms:
            name, _, path = spec.partition("=")
            arm = load(path)
            rows.append((name, arm))
        print(f"{'配置':<26}{'概览 命中/总':>14}{'':>2}{'细节 命中/总':>14}{'':>2}")
        print("-" * 74)
        for name, arm in rows:
            cells = []
            for kind in ("overview", "detail"):
                got, tot = pooled(arm, facts, kind, True)
                cells.append(f"{got:>3}/{tot:<3} {got/tot*100 if tot else 0:>5.1f}%")
            print(f"{name:<26}{cells[0]:>16}{cells[1]:>16}")
        print("\n（池化 + 宽松口径：本评测集概览 30 点、细节 18 点，1 点 = 3.3pp / 5.6pp；"
              "数字是「参考答案的事实点里，有多少被写进了答案」）")
        return 0

    if not (args.base and args.variant):
        ap.error("需要 --base 与 --variant（或用 --arms 做多臂对比）")

    base, var = load(args.base), load(args.variant)

    def agg(arm: Dict[str, Dict], kind: str, loose: bool) -> float:
        vals = [score(r["answer"], facts[q], loose)[0] for q, r in arm.items() if r["kind"] == kind]
        return statistics.mean(vals) if vals else 0.0

    print(f"{'口径':<26}{'概览 基线':>10}{'概览 对照':>10}{'细节 基线':>10}{'细节 对照':>10}")
    print("-" * 70)
    for label, loose in (("严格（history 可比）", False), ("宽松（含措辞差异）", True)):
        print(f"{label:<24}"
              f"{agg(base,'overview',loose):>10.3f}{agg(var,'overview',loose):>12.3f}"
              f"{agg(base,'detail',loose):>10.3f}{agg(var,'detail',loose):>12.3f}")

    # 逐题：只看答案覆盖率有变化的题，并标出「严格判漏但宽松命中」= 纯措辞差异
    detail = []
    print(f"\n{'题':32}{'基线严格/宽松':>16}{'对照严格/宽松':>16}  说明")
    print("-" * 92)
    for q, rv in var.items():
        rb = base.get(q)
        if not rb:
            continue
        s0 = score(rb["answer"], facts[q], False)
        l0 = score(rb["answer"], facts[q], True)
        s1 = score(rv["answer"], facts[q], False)
        l1 = score(rv["answer"], facts[q], True)
        note = ""
        if abs(l1[0] - l0[0]) > 1e-9 or abs(s1[0] - s0[0]) > 1e-9:
            fake = [f for f in (l1[1] if l1[0] > s1[0] else []) if f not in s1[1]]
            if s1[0] < l1[0]:
                note = "严格漏/宽松有 → 措辞差异"
            elif l1[0] < l0[0]:
                note = "宽松口径下也确实变少 → 真回归"
            else:
                note = "有变化"
            detail.append({
                "question": q, "kind": rv["kind"],
                "base_strict": round(s0[0], 3), "base_loose": round(l0[0], 3),
                "var_strict": round(s1[0], 3), "var_loose": round(l1[0], 3),
                "loose_only_hits": fake, "note": note,
            })
            print(f"{q[:30]:32}{s0[0]:>8.2f}/{l0[0]:<7.2f}{s1[0]:>8.2f}/{l1[0]:<7.2f}  {note}")

    if detail:
        fake = sum(1 for d in detail if "措辞" in d["note"])
        real = sum(1 for d in detail if "真回归" in d["note"])
        print(f"\n变化题 {len(detail)} 题：措辞差异 {fake} 题 / 真回归 {real} 题")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(detail, f, ensure_ascii=False, indent=2)
        print(f"明细已写入：{args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
