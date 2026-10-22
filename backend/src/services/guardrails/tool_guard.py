"""工具护栏：参数长度 / URL 安全 / 路径穿越（AM-2）。

在 ToolManager.execute 的统一入口调用，任何工具（含旧 FunctionCalling 路径）
都先过此校验；命中返回失败 ToolResult，不实际执行工具。
"""

import ipaddress
import json
import logging
import re
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from src.config import settings

from .base import GuardrailHit

logger = logging.getLogger("rag_system")

# 会被视为 URL 的参数名（精确匹配或 *_url 后缀）
_URL_KEYS = {"url", "uri", "link", "href", "target_url", "source_url", "webpage"}
# 会被视为路径的参数名（仅这些键做路径穿越检查，避免误伤查询词中的 ../）
_PATH_KEYS = {"path", "file", "filepath", "filename", "dir", "target_path", "save_to"}

_PATH_TRAVERSAL_RE = re.compile(r"(?:^|[\\/])\.\.(?:[\\/]|$)")
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


def _check_url(url: str) -> Optional[str]:
    """校验单个 URL，返回命中的规则名；安全返回 None。"""
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

    if host in _PRIVATE_HOSTNAMES or host.endswith(".localhost"):
        return "private_network"
    if host in _METADATA_IPS:
        return "private_network"

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
        or ip.is_multicast
    ):
        return "private_network"
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
            if _is_url_key(key):
                rule = _check_url(value)
                if rule:
                    return GuardrailHit(
                        kind="tool_argument",
                        rule=rule,
                        detail=f"{tool_name} 的 URL 参数被拒绝（{rule}）",
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
