#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""检查 `.trivyignore` 的豁免是否过期（#32）。

为什么要有这个脚本：`.trivyignore` 里登记的都是「上游约束导致当前升不上去」的 CVE，
它们是**临时豁免**，不是永久白名单。原先的复核日期只写在注释正文里（"复核窗口：2026-11-30"），
机器读不到，于是到期之后没有任何东西会变红——豁免会静悄悄地一直生效。
这正是「看起来有人管，其实没人管」的典型形态。

约定：`.trivyignore` 必须包含一行机器可读字段

    # review-by: 2026-11-30

退出码：0 = 仍在有效期内（含「即将到期」的告警态，不阻断）；1 = 已过期 / 缺字段 / 格式错。

为什么「即将到期」不阻断：离到期还有十几天时把 CI 打红，只会让人习惯性绕过它；
真正需要阻断的是「已经过期还在用」。告警走 GitHub annotation，能看见但不挡合入。
内部 `check()` 仍然返回三态（0/1/2），便于单测区分。

用法：
    python scripts/check_trivyignore.py                # 按今天判断
    python scripts/check_trivyignore.py --today 2026-11-20   # 便于自测
    python scripts/check_trivyignore.py --warn-days 30
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

REVIEW_BY_RE = re.compile(r"^#\s*review-by:\s*(\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)
CVE_RE = re.compile(r"^CVE-\d{4}-\d+$", re.MULTILINE)

DEFAULT_WARN_DAYS = 14


def parse_review_by(text: str) -> dt.date | None:
    match = REVIEW_BY_RE.search(text)
    if not match:
        return None
    try:
        return dt.date.fromisoformat(match.group(1))
    except ValueError:
        return None


def count_cves(text: str) -> int:
    return len(CVE_RE.findall(text))


def check(text: str, today: dt.date, warn_days: int = DEFAULT_WARN_DAYS) -> tuple[int, str]:
    """返回 (退出码, 说明)。纯函数，便于单测。

    缺字段也算失败：如果允许"没写日期就跳过检查"，那么下一次改这个文件的人
    只要删掉那一行就能让检查变成绿灯。
    """
    review_by = parse_review_by(text)
    if review_by is None:
        return 1, (
            "缺少机器可读的复核日期。请在 .trivyignore 顶部加一行：\n"
            "    # review-by: YYYY-MM-DD"
        )

    cves = count_cves(text)
    if cves == 0:
        return 1, "没有登记任何 CVE 条目——如果确实不再需要豁免，请删除这个文件而不是留个空壳"

    days_left = (review_by - today).days
    if days_left < 0:
        return 1, (
            f"豁免已于 {review_by} 到期（已过期 {-days_left} 天），当前仍登记 {cves} 条 CVE。\n"
            "处置：复核这些依赖能否升级；已修复的删掉对应 CVE 行，仍需豁免的把 review-by 往后推并写明原因。"
        )
    if days_left <= warn_days:
        return 2, (
            f"豁免将在 {days_left} 天后（{review_by}）到期，当前登记 {cves} 条 CVE。"
            "请安排复核：能升级的升级，仍需豁免的更新 review-by 与原因。"
        )
    return 0, f"豁免有效期至 {review_by}（还有 {days_left} 天），共 {cves} 条 CVE"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查 .trivyignore 豁免是否过期")
    parser.add_argument("--file", default=".trivyignore", help="豁免文件路径（默认仓库根 .trivyignore）")
    parser.add_argument("--today", default=None, help="覆盖当天日期（YYYY-MM-DD，便于自测）")
    parser.add_argument("--warn-days", type=int, default=DEFAULT_WARN_DAYS, help="提前多少天开始告警")
    args = parser.parse_args(argv)

    path = Path(args.file)
    if not path.exists():
        # 文件不存在不是错误：说明已经不需要豁免了
        print(f"[trivyignore] 未找到 {path}，视为无需豁免")
        return 0

    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    code, message = check(path.read_text(encoding="utf-8"), today, args.warn_days)

    if code == 0:
        print(f"[trivyignore] OK {message}")
    elif code == 2:
        # 告警不阻断：annotation 会在 PR 里显示，但不让 CI 变红
        print(f"[trivyignore] WARN {message}")
        print("::warning file=.trivyignore::" + message.replace("\n", " "))
        return 0
    else:
        print(f"[trivyignore] FAIL {message}", file=sys.stderr)
        print("::error file=.trivyignore::" + message.replace("\n", " "), file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
