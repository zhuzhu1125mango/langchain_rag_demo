"""输出护栏：凭证 / 系统提示泄漏脱敏（AM-2）。

在管线的 chunk 统一出口调用：命中即把敏感片段替换为 REDACTION_TEXT，
同时由管线修正 state.final_answer，保证 SSE、落库、trace 三处一致。
"""

import logging
import re
from typing import List, Optional, Tuple

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


class StreamingOutputGuard:
    """流式输出脱敏：跨 chunk 的密钥/系统提示不会因切片而漏检。

    策略：无触发器时仅保留极短尾部（8 字符）以捕获跨切片的触发前缀；
    一旦发现触发器（sk-/AKIA/Bearer/私钥/DB URL/系统提示首句），从触发器
    位置起暂存，直到 flush 或超过 max_buffer 后释放已脱敏前缀。这样正常
    文本几乎无延迟，可疑片段最多延迟 max_buffer 字符。
    """

    _TRIGGERS = (
        "sk-",
        "AKIA",
        "Bearer ",
        "-----BEGIN",
        "postgres://",
        "postgresql://",
        "你是一个严谨的智能助手",
    )

    def __init__(self, guard: Optional[OutputGuard] = None, holdback: int = 8, max_buffer: int = 256):
        self.guard = guard or _default_guard
        self.holdback = holdback
        self.max_buffer = max_buffer
        self.buffer = ""

    def _enabled(self) -> bool:
        return bool(
            settings.guardrail.GUARDRAIL_ENABLED
            and settings.guardrail.GUARDRAIL_OUTPUT_ENABLED
        )

    def feed(self, chunk: str) -> Tuple[str, List[str]]:
        if not chunk:
            return "", []
        if not self._enabled():
            return chunk, []

        self.buffer += chunk
        trigger_idx = -1
        for trigger in self._TRIGGERS:
            idx = self.buffer.find(trigger)
            if idx != -1 and (trigger_idx == -1 or idx < trigger_idx):
                trigger_idx = idx

        if trigger_idx == -1:
            if len(self.buffer) <= self.holdback:
                return "", []
            emit = self.buffer[:-self.holdback]
            self.buffer = self.buffer[-self.holdback:]
        else:
            if trigger_idx > 0:
                emit = self.buffer[:trigger_idx]
                self.buffer = self.buffer[trigger_idx:]
            else:
                emit = ""
            if len(self.buffer) > self.max_buffer:
                cut = len(self.buffer) - self.max_buffer
                prefix, prefix_hits = self.guard.redact(self.buffer[:cut])
                self.buffer = self.buffer[cut:]
                safe, hits = self.guard.redact(emit + prefix)
                return safe, prefix_hits + hits

        safe, hits = self.guard.redact(emit)
        return safe, hits

    def flush(self) -> Tuple[str, List[str]]:
        if not self.buffer:
            return "", []
        text, hits = self.guard.redact(self.buffer)
        self.buffer = ""
        return text, hits


def get_output_guard() -> OutputGuard:
    """获取进程级输出护栏实例（无状态，可复用）。"""
    return _default_guard
