#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""扫文档里的「无口径判断」（#34 的可选加分项）。

背景：这个项目里出现过一类具体的失真——把**没测过的判断**写成结论，和实测数字并列：

    「无索引，万级会话下可接受」   ← 没测过，却和「hit@5 = 0.869」排在一起

读者分不出哪句是量的、哪句是猜的。所以定一条规则：**判断句要么带口径，要么标（未测）**。

只抓「规模/性能类」的少数几个词，不是通用 lint：
- 词表窄 → 误报少 → 才会真的有人看；
- 「可接受」这类词在设计文档里大量用于表达取舍（"成本高，raw 兜底可接受"），
  本身不是问题，所以**只检测它和规模词一起出现**的情况。

排除：`docs/archive/`（冻结文档，不回改）、代码块里的内容不判（那是示例/配置）。

用法：
    python scripts/check_docs_claims.py
    python scripts/check_docs_claims.py --paths README.md docs
退出码：0 = 没发现；1 = 发现无标注判断（会打印 文件:行号 + 原因）。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# 词表刻意很短：每多一个词，就多一批「其实没问题的句子」，最后没人看
BANNED = {
    "性能足够": "性能判断",
    "不会成为瓶颈": "容量判断",
    "足够快": "性能判断",
    "应该没问题": "未验证的结论",
    "理论上没问题": "未验证的结论",
}

# 「规模 + 主观结论」的组合才判：单独出现「可接受」不算问题
SCALE_WORDS = ("万级", "十万级", "百万级", "千级", "高并发", "大规模")
SUBJECTIVE = ("可接受", "没问题", "够用", "顶得住", "扛得住")

# 出现这些词说明作者已经交代了口径或不确定性，放行
QUALIFIERS = ("未测", "待测", "实测", "压测", "报告", "http", "docs/benchmarks", "（见", "(见")
# 「禁止/不许用 X 这类词」是在定规矩，不是在断言——不跳过的话，写作约定那一页
# 自己就会被这条 lint 判红（第一次跑就踩到了）。
QUALIFIERS += ("不许", "禁止", "不要用", "别用")

SKIP_DIRS = {"archive", "node_modules", ".git", "audit"}
# audit/ 也跳过：对抗性审计报告的工作方式就是**引用问题原文**（"这里写着'万级可接受'"），
# 引用会被误判成新的问题。审计文档的可信度靠报告本身，不靠这条 lint。
# 需要时可以直接传单个文件：`--paths docs/audit/xxx.md`（显式文件不走目录跳过规则）。


def iter_markdown(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_file() and path.suffix == ".md":
            files.append(path)
        elif path.is_dir():
            for md in sorted(path.rglob("*.md")):
                if any(part in SKIP_DIRS for part in md.parts):
                    continue
                files.append(md)
    return files


def _strip_code_fences(text: str) -> list[str]:
    """把 ``` 代码块里的行换成空串（保持行号对齐）。"""
    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append("")
            continue
        out.append("" if in_fence else line)
    return out


def find_issues(text: str) -> list[tuple[int, str, str]]:
    """返回 [(行号, 命中片段, 原因)]。纯函数，便于单测。"""
    issues: list[tuple[int, str, str]] = []
    for idx, line in enumerate(_strip_code_fences(text), start=1):
        if any(q in line for q in QUALIFIERS):
            continue
        hit = next((w for w in BANNED if w in line), None)
        if hit:
            issues.append((idx, hit, BANNED[hit]))
            continue
        if any(s in line for s in SCALE_WORDS) and any(w in line for w in SUBJECTIVE):
            scale = next(s for s in SCALE_WORDS if s in line)
            issues.append((idx, scale, f"规模判断（{scale} + 主观结论）"))
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查文档里的无口径判断")
    parser.add_argument("--paths", nargs="+", default=["README.md", "docs"], help="要扫描的文件/目录")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    targets = [root / p for p in args.paths]

    total = 0
    for md in iter_markdown(targets):
        try:
            text = md.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_no, snippet, reason in find_issues(text):
            rel = md.relative_to(root).as_posix()
            print(f"{rel}:{line_no}: {reason}「{snippet}」——要么给口径（命令/报告链接），要么标（未测）")
            total += 1

    if total:
        print(f"\n共 {total} 处。约定见 docs/README.md「写作约定」。", file=sys.stderr)
        return 1
    print("[docs-claims] OK 未发现无口径的规模/性能判断")
    return 0


if __name__ == "__main__":
    sys.exit(main())
