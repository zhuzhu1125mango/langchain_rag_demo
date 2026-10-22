"""输出护栏：凭证 / 系统提示泄漏脱敏（AM-2）。

在管线的 chunk 统一出口调用：命中即把敏感片段替换为 REDACTION_TEXT，
同时由管线修正 state.final_answer，保证 SSE、落库、trace 三处一致。
"""

import logging
import re
from typing import List, Tuple

from src.config import settings

from .base import REDACTION_TEXT

logger = logging.getLogger("rag_system")

# 规则顺序即替换顺序；只匹配高置信度模式，避免大面积误伤正常回答。
_SECRET_RULES = (
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"), "private_key"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"), "openai_key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws_key"),
    (re.compile(r"postgres(?:ql)?://[^\s:@/]+:[^\s@/]+@", re.I), "db_credentials"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}\b"), "bearer_token"),
    # 本系统提示词特征句逐字外泄（仅精确长句，避免误伤知识库中对该系统的介绍）
    (
        re.compile(r"你是一个严谨的智能助手[，,]\s*可以调用工具来回答用户问题"),
        "system_prompt_leak",
    ),
)


class OutputGuard:
    """输出侧护栏（无状态）。"""

    def redact(self, text: str) -> Tuple[str, List[str]]:
        """脱敏文本，返回 (脱敏后文本, 命中的规则名列表)。"""
        if not settings.guardrail.GUARDRAIL_ENABLED:
            return text, []
        if not settings.guardrail.GUARDRAIL_OUTPUT_ENABLED:
            return text, []
        if not text:
            return text, []

        hits: List[str] = []
        result = text
        for pattern, rule in _SECRET_RULES:
            replaced, count = pattern.subn(REDACTION_TEXT, result)
            if count:
                hits.append(rule)
                result = replaced
        return result, hits


_default_guard = OutputGuard()


def get_output_guard() -> OutputGuard:
    """获取进程级输出护栏实例（无状态，可复用）。"""
    return _default_guard
