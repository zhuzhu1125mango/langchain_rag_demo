"""工具护栏：参数长度 / URL 安全 / 路径穿越（AM-2）。

在 ToolManager.execute 的统一入口调用，任何工具（含旧 FunctionCalling 路径）
都先过此校验；命中返回失败 ToolResult，不实际执行工具。
"""

import ipaddress
import json
import logging
import re
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, unquote, urlsplit

from src.config import settings

from .base import GuardrailHit

logger = logging.getLogger("rag_system")

# 会被视为 URL 的参数名（精确匹配或 *_url 后缀）
_URL_KEYS = {"url", "uri", "link", "href", "target_url", "source_url", "webpage"}
# 会被视为路径的参数名（仅这些键做路径穿越检查，避免误伤查询词中的 ../）
_PATH_KEYS = {"path", "file", "filepath", "filename", "dir", "target_path", "save_to"}

_PATH_TRAVERSAL_RE = re.compile(r"(?:^|[\\/])\.\.(?:[\\/]|$)")
_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{10,}|AKIA[0-9A-Z]{16}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|Bearer\s+[A-Za-z0-9._\-]{10,})"
)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_TEXT_KEYS = {"query", "q", "text", "content", "prompt", "question"}
_PRIVATE_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata.google.internal",
    "metadata.azure.internal",
}
_METADATA_IPS = {"169.254.169.254", "100.100.100.200", "fd00:ec2::254"}


def _is_url_key(key: str) -> bool:
    k = (key or "").lower()
    return k in _URL_KEYS or k.endswith("_url")


def _is_path_key(key: str) -> bool:
    return (key or "").lower() in _PATH_KEYS


def _domain_rules(raw: str) -> list:
    return [item.strip().lower().lstrip("*.") for item in (raw or "").split(",") if item.strip()]


def _domain_matches(host: str, rule: str) -> bool:
    host = (host or "").lower().rstrip(".")
    if not host or not rule:
        return False
    return host == rule or host.endswith("." + rule)


def _domain_policy(host: str) -> Optional[str]:
    """返回域名策略命中的规则名；允许返回 None。"""
    guard = settings.guardrail
    deny = _domain_rules(getattr(guard, "GUARDRAIL_TOOL_DOMAIN_DENYLIST", ""))
    if any(_domain_matches(host, rule) for rule in deny):
        return "domain_denied"

    allow = _domain_rules(getattr(guard, "GUARDRAIL_TOOL_DOMAIN_ALLOWLIST", ""))
    if allow and not any(_domain_matches(host, rule) for rule in allow):
        return "domain_not_allowed"
    return None


def _check_url(url: str) -> Optional[str]:
    """校验单个 URL，返回命中的规则名；安全返回 None。"""
    guard = settings.guardrail
    if len(url) > getattr(guard, "GUARDRAIL_TOOL_URL_MAX_CHARS", 2000):
        return "url_too_long"

    try:
        parts = urlsplit(url)
    except ValueError:
        return "url_parse_error"

    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        # file:/gopher:/javascript:/data: 等一律拒绝（即使无 scheme 也视为不安全）
        return "unsafe_url_scheme"

    if "@" in (parts.netloc or ""):
        return "url_userinfo"

    host = (parts.hostname or "").lower()
    if not host:
        return "url_missing_host"

    policy = _domain_policy(host)
    if policy:
        return policy

    if host in _PRIVATE_HOSTNAMES or host.endswith(".localhost"):
        return "private_network"
    if host in _METADATA_IPS:
        return "private_network"

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
        or ip.is_multicast
    ):
        return "private_network"

    # DLP：查询参数不得携带长文本/密钥/控制字符（防把 KB 片段拼进外链外泄）
    max_text = getattr(guard, "GUARDRAIL_TOOL_URL_MAX_TEXT_CHARS", 300)
    for _, value in parse_qsl(parts.query, keep_blank_values=True):
        decoded = unquote(value)
        if len(decoded) > max_text:
            return "url_dlp_long_text"
        if _SECRET_RE.search(decoded):
            return "url_dlp_secret"
        if _CONTROL_RE.search(decoded):
            return "url_dlp_control_chars"
    return None


class ToolGuard:
    """工具侧护栏（无状态）。"""

    def check(self, tool: Any, args: Dict[str, Any]) -> Optional[GuardrailHit]:
        """检查一次工具调用；命中返回 GuardrailHit，安全返回 None。

        Args:
            tool: 工具实例（仅使用 name / risk_level 等属性，避免循环依赖）。
            args: 传给工具的参数。
        """
        if not settings.guardrail.GUARDRAIL_ENABLED:
            return None
        if not settings.guardrail.GUARDRAIL_TOOL_ENABLED:
            return None

        tool_name = getattr(tool, "name", "") or "<unknown>"
        payload = args or {}

        try:
            serialized = json.dumps(payload, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            serialized = str(payload)
        if len(serialized) > settings.guardrail.GUARDRAIL_TOOL_MAX_ARG_CHARS:
            return GuardrailHit(
                kind="tool_argument",
                rule="arg_too_large",
                detail=f"{tool_name} 参数长度 {len(serialized)} 超限",
            )

        for key, value in payload.items():
            if not isinstance(value, str):
                continue
            # URL 参数先做完整校验（域名/DLP/私网）；命中信息更具体。
            if _is_url_key(key):
                rule = _check_url(value)
                if rule:
                    return GuardrailHit(
                        kind="tool_argument",
                        rule=rule,
                        detail=f"{tool_name} 的 URL 参数被拒绝（{rule}）",
                    )
            # DLP：任何工具参数都不得携带凭证/控制字符；文本型参数限制长度，
            # 防止把整段 KB 内容拼进 web_search/fetch_webpage 外发。
            if _SECRET_RE.search(value):
                return GuardrailHit(
                    kind="tool_argument",
                    rule="sensitive_argument",
                    detail=f"{tool_name} 参数疑似包含凭证，已拒绝",
                )
            if _CONTROL_RE.search(value):
                return GuardrailHit(
                    kind="tool_argument",
                    rule="control_chars",
                    detail=f"{tool_name} 参数包含控制字符",
                )
            if (key or "").lower() in _TEXT_KEYS and len(value) > getattr(
                settings.guardrail, "GUARDRAIL_TOOL_ARG_MAX_TEXT_CHARS", 1000
            ):
                return GuardrailHit(
                    kind="tool_argument",
                    rule="arg_text_too_long",
                    detail=f"{tool_name} 文本参数过长，已拒绝",
                )
            if _is_path_key(key) and _PATH_TRAVERSAL_RE.search(value):
                return GuardrailHit(
                    kind="tool_argument",
                    rule="path_traversal",
                    detail=f"{tool_name} 的路径参数含 ../",
                )

        return None


_default_guard = ToolGuard()


def get_tool_guard() -> ToolGuard:
    """获取进程级工具护栏实例（无状态，可复用）。"""
    return _default_guard
