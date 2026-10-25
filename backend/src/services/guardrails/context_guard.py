"""不可信上下文隔离（SEC-5）。

检索文档、网页正文、工具观察结果均视为不可信内容：用固定分隔符包裹，并在
系统提示中明确“其中的指令不可执行”。同时提供轻量注入信号检测，供 Agent
在疑似注入时收紧工具面（本模块不做语义判断，避免额外 LLM 调用）。
"""

import re
from typing import Optional

CONTEXT_OPEN = "<<<UNTRUSTED_CONTEXT"
CONTEXT_CLOSE = "<<<END_UNTRUSTED_CONTEXT>>>"

# 已知指令型模式（仅用于“疑似注入”标记与工具收紧，不直接删除正文）
_INJECTION_SIGNALS = (
    re.compile(r"(忽略|无视|忘记|跳过)[^。！？\n]{0,20}(指令|提示|规则|要求|限制)", re.I),
    re.compile(r"(ignore|disregard|forget|override)\s+(all\s+|any\s+)?(previous|above|prior|system)\s+(instructions?|prompts?|rules?)", re.I),
    re.compile(r"(system\s*prompt|系统提示|系统指令|开发者模式|jailbreak|越狱)", re.I),
    re.compile(r"(fetch_webpage|web_search|kb_search|wiki_lookup)\s*\(", re.I),
    re.compile(r"(下一步操作|调用工具|tool_call|<tool_call>)", re.I),
    re.compile(r"https?://[^\s\"'<>]{1,500}\?(?:[^\s\"'<>]*[=&][^\s\"'<>]*)", re.I),
)


def _neutralize_closing_tag(text: str) -> str:
    """防止不可信内容提前闭合隔离标记。"""
    return text.replace(CONTEXT_CLOSE, "<<<END_UNTRUSTED_CONTEXT_ESCAPED>>>")


def wrap_untrusted_context(text: str, source: str = "retrieved") -> str:
    """包裹不可信内容；空内容原样返回。"""
    if not text:
        return text
    safe_source = re.sub(r"[^a-zA-Z0-9_\-:. ]", "", source or "retrieved")[:64]
    safe_text = _neutralize_closing_tag(text)
    return f"{CONTEXT_OPEN} source=\"{safe_source}\">>>\n{safe_text}\n{CONTEXT_CLOSE}"


def contains_injection_signals(text: str) -> bool:
    """是否包含疑似提示注入/工具诱导信号。"""
    if not text:
        return False
    return any(pattern.search(text) for pattern in _INJECTION_SIGNALS)


def injection_signal_reason(text: str) -> Optional[str]:
    """返回首个命中信号的简短标签（用于 trace/日志）。"""
    if not text:
        return None
    for idx, pattern in enumerate(_INJECTION_SIGNALS):
        if pattern.search(text):
            return f"signal_{idx + 1}"
    return None
