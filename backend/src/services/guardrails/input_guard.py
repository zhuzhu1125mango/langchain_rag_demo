"""输入护栏：提示注入 / 越权指令 / 超长 / 控制字符（AM-2）。

判定全部为确定性规则（关键词共现 + 长度），零 LLM 调用。命中即由管线走
「拒答」分支（answer_type=guardrail_blocked），不进入检索/生成。
"""

import logging
import re
import unicodedata
from typing import Optional

from src.config import settings

from .base import GuardrailHit

logger = logging.getLogger("rag_system")

# 输入护栏命中后的统一拒答文案（answer_type=guardrail_blocked，不写语义缓存）
INPUT_BLOCK_MESSAGE = (
    "抱歉，你的输入包含不安全或越权指令，已被安全护栏拦截。请改为正常提问。"
)

# 控制字符（排除常见的 \n \r \t），少量出现可能来自粘贴，超过阈值才拦截
_CONTROL_CHAR_LIMIT = 20
_CONTROL_CHARS = {chr(i) for i in range(32)} - {"\n", "\r", "\t"}

# 注入/越权特征：只匹配「明确指令型」表达，避免把正常提问或引用文档误判。
# 每条规则要求关键词共现（动作 + 对象），而不是单个词命中。
_INJECTION_RULES = (
    (
        re.compile(r"(忽略|无视|忘记|忘掉|抛弃|跳过)[^。！？\n]{0,20}(指令|提示|规则|要求|设定|限制|指示)"),
        "ignore_instructions",
        "要求忽略既有指令",
    ),
    (
        re.compile(r"(泄露|透露|输出|展示|打印|重复|告诉我)[^。！？\n]{0,20}(系统提示|系统指令|提示词|system\s*prompt)", re.I),
        "system_prompt_extraction",
        "要求套取系统提示",
    ),
    (
        re.compile(r"(开发者模式|调试模式|越狱模式|越狱|jailbreak|developer\s*mode)", re.I),
        "jailbreak_mode",
        "要求进入越狱/开发者模式",
    ),
    (
        re.compile(r"(从现在起|现在开始|接下来)[^。！？\n]{0,12}(你是|你扮演|扮演)"),
        "role_override",
        "要求覆盖助手身份",
    ),
    (
        re.compile(
            r"(ignore|disregard|forget|override)\s+(all\s+|any\s+)?"
            r"(previous|above|prior|earlier|system)\s+(instructions?|prompts?|rules?)",
            re.I,
        ),
        "ignore_instructions_en",
        "ignore previous instructions",
    ),
    (
        re.compile(
            r"(reveal|show|print|repeat|output)\s+(your\s+|the\s+)?"
            r"(system\s*prompt|system\s*instructions?|hidden\s*prompt)",
            re.I,
        ),
        "system_prompt_extraction_en",
        "system prompt extraction",
    ),
)


class InputGuard:
    """输入侧护栏（无状态，可在进程内复用）。"""

    def check(self, text: str) -> Optional[GuardrailHit]:
        """检查用户输入，命中返回 GuardrailHit，否则返回 None。"""
        if not settings.guardrail.GUARDRAIL_ENABLED:
            return None
        if not settings.guardrail.GUARDRAIL_INPUT_ENABLED:
            return None

        raw = text or ""
        if not raw.strip():
            return None

        # NFKC 归一化：规避全角/兼容字符绕过关键词匹配
        normalized = unicodedata.normalize("NFKC", raw)

        if len(normalized) > settings.guardrail.GUARDRAIL_INPUT_MAX_CHARS:
            return GuardrailHit(
                kind="input_too_long",
                rule="input_too_long",
                detail=f"长度 {len(normalized)} 超过上限 {settings.guardrail.GUARDRAIL_INPUT_MAX_CHARS}",
            )

        if "\x00" in normalized:
            return GuardrailHit(
                kind="control_chars", rule="null_byte", detail="输入包含 NUL 字符"
            )
        control_count = sum(1 for ch in normalized if ch in _CONTROL_CHARS)
        if control_count >= _CONTROL_CHAR_LIMIT:
            return GuardrailHit(
                kind="control_chars",
                rule="control_char_flood",
                detail=f"控制字符 {control_count} 个",
            )

        for pattern, rule, detail in _INJECTION_RULES:
            if pattern.search(normalized):
                return GuardrailHit(kind="prompt_injection", rule=rule, detail=detail)

        return None


_default_guard = InputGuard()


def get_input_guard() -> InputGuard:
    """获取进程级输入护栏实例（无状态，可复用）。"""
    return _default_guard
