"""护栏公共类型与常量。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class GuardrailHit:
    """一次护栏命中的判定结果。

    Attributes:
        kind: 护栏类别（prompt_injection / tool_argument / output_secret 等）。
        rule: 具体规则名，用于 trace 埋点与日志检索。
        detail: 便于排查的简短说明（不回显用户原文，避免把敏感内容写进日志）。
    """

    kind: str
    rule: str
    detail: str = ""


# 输出护栏命中后的替换文本（SSE、落库、trace 三处保持一致）
REDACTION_TEXT = "[已屏蔽敏感内容]"
