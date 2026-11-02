"""两臂配对显著性检验：这个差距是真的，还是 1 道题的抖动？

背景：评测集只有 64 道有答案题，**1 道题 = 1.6pp**。hit@3 从 0.922 变成 0.938
只差一道题，直接把 "0.938 > 0.922" 写进结论是不诚实的——同样的改动换个随机种子
就能反向。这个脚本用 McNemar 精确检验回答「差异是否超出噪声」。

用法：
    cd backend && uv run python scripts/ab_significance.py \
        --report ../docs/benchmarks/channel-fusion-2026-11-02.json --a dense --b sfuse70

    # 一次比多组
    uv run python scripts/ab_significance.py --report <json> --pairs "dense:sfuse70,wrrf50:sfuse50"

判据（双侧精确二项检验，等价于 McNemar 精确版）：
    只统计**不一致对**：A 中 B 不中（b）、B 中 A 不中（c）。两者都中/都不中的题
    不携带信息，直接丢掉——这正是配对检验相对「两个比例相减」的优势。
    p < 0.05 才可以说「B 确实更好」；否则只能说「没有证据表明有差异」。
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DEFAULT_REPORT = Path("../docs/benchmarks/channel-fusion-2026-11-02.json")


def mcnemar_exact(b: int, c: int) -> float:
    """McNemar 精确检验的双侧 p 值（二项分布，p=0.5）。

    参数 b/c 是两类不一致对的数量。n=0（两臂逐题完全一致）时 p=1.0。
    """
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2**n)
    return min(1.0, 2.0 * tail)


def load_arm(report_path: Path, arm: str) -> Tuple[Dict[str, bool], Dict]:
    """读取某臂的逐题命中表。返回 ({question: hit}, 汇总指标)。"""
    data = json.loads(report_path.read_text(encoding="utf-8"))
    arms = data.get("arms", {})
    if arm not in arms:
        raise KeyError(f"报告里没有臂 {arm!r}，可选：{', '.join(arms)}")
    payload = arms[arm]
    per_question = payload.get("per_question")
    if not per_question:
        raise KeyError(
            f"臂 {arm!r} 的报告没有 per_question 字段。它是后加的："
            "请用当前版本的 run_channel_ab.py 重新生成报告。"
        )
    return {row["question"]: bool(row["hit"]) for row in per_question}, payload


def compare(
    a_hits: Dict[str, bool],
    b_hits: Dict[str, bool],
    a_name: str,
    b_name: str,
    show_flips: int = 5,
) -> Dict:
    """配对比较两臂。只统计两臂都出现过的题（防止数据集版本不一致）。"""
    questions = [q for q in a_hits if q in b_hits]
    both = sum(1 for q in questions if a_hits[q] and b_hits[q])
    only_a = [q for q in questions if a_hits[q] and not b_hits[q]]
    only_b = [q for q in questions if b_hits[q] and not a_hits[q]]
    neither = sum(1 for q in questions if not a_hits[q] and not b_hits[q])

    p_value = mcnemar_exact(len(only_a), len(only_b))
    n = len(questions)
    return {
        "n": n,
        "a": a_name,
        "b": b_name,
        "hit_a": sum(1 for q in questions if a_hits[q]) / n if n else 0.0,
        "hit_b": sum(1 for q in questions if b_hits[q]) / n if n else 0.0,
        "both": both,
        "only_a": only_a,
        "only_b": only_b,
        "neither": neither,
        "p_value": p_value,
        "one_question_pp": round(100.0 / n, 2) if n else 0.0,
        "delta_pp": round((len(only_b) - len(only_a)) * 100.0 / n, 2) if n else 0.0,
        "flips": {"only_b": only_b[:show_flips], "only_a": only_a[:show_flips]},
    }


def print_result(r: Dict) -> None:
    verdict = (
        "**显著**：差异超出噪声"
        if r["p_value"] < 0.05
        else "在噪声内：没有证据表明两者有差异"
    )
    print(f"[{r['a']} vs {r['b']}]  n={r['n']} 题（1 题 = {r['one_question_pp']}pp）")
    print(f"  hit: {r['a']} = {r['hit_a']:.3f}   {r['b']} = {r['hit_b']:.3f}   Δ = {r['delta_pp']:+.2f}pp")
    print(f"  一致命中 {r['both']} / 一致未中 {r['neither']}")
    print(f"  仅 {r['a']} 命中 {len(r['only_a'])} / 仅 {r['b']} 命中 {len(r['only_b'])}")
    print(f"  McNemar 精确检验 p = {r['p_value']:.4f} → {verdict}")
    for label, items in (("B 新增命中", r["flips"]["only_b"]), ("B 丢失命中", r["flips"]["only_a"])):
        for q in items:
            print(f"    {label}: {q}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="两臂配对显著性检验（McNemar 精确检验）")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--a", default="dense", help="基线臂")
    parser.add_argument("--b", default="sfuse70", help="待验证臂")
    parser.add_argument("--pairs", default=None, help='多组比较，如 "dense:sfuse70,wrrf50:sfuse50"，优先于 --a/--b')
    parser.add_argument("--show-flips", type=int, default=5, help="每组最多列出几道翻转题")
    parser.add_argument("--json", type=Path, default=None, help="把结果写成 JSON")
    args = parser.parse_args()

    pairs: List[Tuple[str, str]] = (
        [tuple(p.split(":")) for p in args.pairs.split(",") if ":" in p]  # type: ignore[misc]
        if args.pairs
        else [(args.a, args.b)]
    )

    results = []
    for a_name, b_name in pairs:
        try:
            a_hits, _ = load_arm(args.report, a_name.strip())
            b_hits, _ = load_arm(args.report, b_name.strip())
        except KeyError as exc:
            # 臂名写错是常见操作失误，给一句能照着改的提示，别甩 traceback
            print(f"[skip] {exc.args[0]}")
            continue
        r = compare(a_hits, b_hits, a_name.strip(), b_name.strip(), args.show_flips)
        print_result(r)
        results.append(r)

    if not results:
        print("没有任何一组比较成功，检查 --pairs 里的臂名。")
        return 2

    if args.json:
        args.json.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"结果已写入 {args.json}")

    # 全部在噪声内时退出 0，有任一显著差异也退出 0：这是分析工具，不是卡点。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
