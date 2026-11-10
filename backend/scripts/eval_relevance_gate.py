"""离线评估「相关性门禁」阈值：用探针数据判断该不该拒绝回答。

背景
----
线上早就有一道门禁（`KB_RELEVANCE_SCORE_THRESHOLD`，默认 0.3）：把最终选中的片段
按 `rerank_score` 与阈值比，全都低于阈值就判定「知识库里没有相关内容」，
走 `KB_MISS_TEMPLATE` 显式拒答。所以「低置信兜底」不是待加的新机制，
而是**这个数字该取多少**。

2026-11-10 的提议是「top1 < 0.15 不硬答」。本脚本用生产探针的逐题数据回答两件事：

1. 把阈值从 0.3 降到 0.15，判定结果变不变？
2. 任何一个阈值，能不能做到「只拒该拒的、不拒能答的」？

口径
----
- 样本：`probe_prod_stages.py` 的 24 题（生产索引、生产重排、生产窗口）；
- 「该拒」= 该题 top-3 的事实点覆盖率 == 0（三块拼起来一个事实都没答到）；
- 「误拒」= 覆盖率 > 0 却被判不相关 —— 这是**能回答却拒答**，比答错更伤用户信任；
- 门禁实现按 `pipeline/retrieval.py` 的真实逻辑：`any(score >= t) or avg >= t`，
  由于 `max >= avg`，等价于 `max(score) >= t`。

用法
----
    python scripts/eval_relevance_gate.py --probe <探针JSON> [--probe ...]
    python scripts/eval_relevance_gate.py --probe x.json --sweep 0,0.05,0.1,0.15,0.2,0.3,0.5,0.8
    python scripts/eval_relevance_gate.py --probe x.json --check 0.15   # 只判某个阈值

退出码：0 = 结论成立（无「能答却拒答」）/ 1 = 查出了误拒 / 2 = 输入不可用
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

DEFAULT_SWEEP = [0.0, 0.01, 0.02, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 0.7, 0.9]


def load_samples(path: Path, stage: str = "rerank_n3") -> List[Dict[str, Any]]:
    """从探针报告里取 (题目, 选中片段的 rerank 分, 覆盖率)。

    探针报告既可能是生产版（`rerank_score` + `coverage` + `hits`），
    也可能是本地版（同名字段、facts 是列表），字段口径已对齐，这里只做兼容读取。
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data.get("rows") or []
    out: List[Dict[str, Any]] = []
    for r in rows:
        st = (r.get("stages") or {}).get(stage) or {}
        picked = st.get("picked") or []
        # 生产探针写 `rerank_score`，早期本地探针写 `score`：两者都是重排分，
        # 取不到就退化成 0 会让「任何阈值都全拒」——那种结论看着像发现，其实是读错字段
        scores = [float(p.get("rerank_score", p.get("score")) or 0.0) for p in picked]
        if not scores:
            continue
        out.append(
            {
                "question": r.get("question", ""),
                "kind": r.get("kind", ""),
                "scores": scores,
                "coverage": float(st.get("coverage") or 0.0),
            }
        )
    return out


def gate_decision(scores: Sequence[float], threshold: float) -> bool:
    """门禁判定：返回 True 表示「有相关内容，放行」。

    与 `_calculate_relevance` 一致：`any(s >= t) or avg >= t`；因为 max >= avg，
    实际等价于 `max(scores) >= t`。这里两种写法都保留，避免以后有人改动实现后
    本脚本悄悄失去可比性。
    """
    if not scores:
        return False
    avg = sum(scores) / len(scores)
    return any(s >= threshold for s in scores) or avg >= threshold


def evaluate(samples: Sequence[Dict[str, Any]], threshold: float) -> Dict[str, Any]:
    """在给定阈值下统计：拒答数、其中误拒数、漏拒数（该拒却放行）。"""
    refused, false_refuse, missed = [], [], []
    for s in samples:
        passed = gate_decision(s["scores"], threshold)
        should_refuse = s["coverage"] == 0.0
        if not passed:
            refused.append(s)
            if not should_refuse:
                false_refuse.append(s)
        elif should_refuse:
            missed.append(s)
    return {
        "threshold": threshold,
        "n": len(samples),
        "refused": len(refused),
        "false_refuse": len(false_refuse),
        "missed": len(missed),
        "answered": len(samples) - len(refused),
        "false_refuse_questions": [s["question"] for s in false_refuse],
        "min_answered_score": min(
            (max(s["scores"]) for s in samples if gate_decision(s["scores"], threshold)),
            default=None,
        ),
    }


def safe_thresholds(results: Sequence[Dict[str, Any]]) -> List[float]:
    """没有误拒的阈值里，能拒掉最多「该拒」题的那个。

    这是本脚本唯一推荐的取值方式：**先在能答的题上找下界，再看这个下界能拒掉什么**。
    反过来（先定想要拒掉多少）就会像 0.15 那样，把能答的题一起拒掉。
    """
    ok = [r for r in results if r["false_refuse"] == 0]
    if not ok:
        return []
    best = max(r["refused"] for r in ok)
    return [r["threshold"] for r in ok if r["refused"] == best]


def _fmt(v: Optional[float]) -> str:
    return "-" if v is None else f"{v:.3f}"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="离线评估相关性门禁阈值")
    parser.add_argument("--probe", action="append", default=[], help="探针报告 JSON，可重复")
    parser.add_argument("--stage", default="rerank_n3", help="取哪一级的结果（默认生产等价的 n3）")
    parser.add_argument("--sweep", default="", help="逗号分隔的阈值列表")
    parser.add_argument("--check", type=float, default=None, help="只判定某一个阈值")
    parser.add_argument("--report", default="", help="把结果写成 JSON")
    args = parser.parse_args(argv)

    if not args.probe:
        print("至少给一个 --probe", file=sys.stderr)
        return 2

    samples: List[Dict[str, Any]] = []
    for p in args.probe:
        try:
            part = load_samples(Path(p), args.stage)
        except Exception as exc:  # noqa: BLE001 - 输入问题统一走退出码 2
            print(f"读取失败 {p}: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        print(f"{p}: {len(part)} 题（{args.stage}）")
        samples.extend(part)

    if not samples:
        print("探针报告里没有可用样本", file=sys.stderr)
        return 2

    sweep = [float(x) for x in args.sweep.split(",")] if args.sweep else DEFAULT_SWEEP
    if args.check is not None:
        sweep = [args.check]

    results = [evaluate(samples, t) for t in sweep]

    print(f"\n样本 {len(samples)} 题，其中「该拒」（覆盖率 0）{sum(1 for s in samples if s['coverage'] == 0)} 题")
    print(f"{'阈值':>6}{'拒答':>6}{'误拒':>6}{'漏拒':>6}{'放行':>6}  说明")
    for r in results:
        note = ""
        if r["false_refuse"]:
            note = "误拒：" + "、".join(q[:12] for q in r["false_refuse_questions"])
        print(
            f"{r['threshold']:>6.2f}{r['refused']:>6}{r['false_refuse']:>6}"
            f"{r['missed']:>6}{r['answered']:>6}  {note}"
        )

    safe = safe_thresholds(results)
    if safe:
        best = next(r for r in results if r["threshold"] == safe[0])
        print(
            f"\n无「能答却拒答」的最大拒答阈值：{safe[0]:.2f}"
            f"（拒 {best['refused']} 题，其中该拒 {best['refused'] - best['false_refuse']} 题；"
            f"漏拒 {best['missed']} 题）"
        )
    else:
        print("\n扫描范围内不存在「零误拒」的阈值：任何拒绝都会连带拒掉能答的题。")

    if args.report:
        Path(args.report).write_text(
            json.dumps({"samples": len(samples), "stage": args.stage, "results": results,
                        "safe_thresholds": safe}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"报告已写入：{args.report}")

    if args.check is not None:
        checked = results[0]
        if checked["false_refuse"]:
            print(
                f"\n[FAIL] 阈值 {args.check} 会拒掉 {checked['false_refuse']} 道**能答**的题："
                + "、".join(checked["false_refuse_questions"]),
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
