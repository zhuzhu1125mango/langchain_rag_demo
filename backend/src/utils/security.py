"""安全相关工具函数。

提供密钥强度校验、弱密钥检测、URL 安全校验等通用安全能力。
"""

import ipaddress
import re
import socket
from typing import Optional, Set
from urllib.parse import urlparse


# 常见弱密钥黑名单（大小写不敏感）
_WEAK_SECRET_KEYWORDS: Set[str] = {
    "secret",
    "password",
    "123456",
    "12345678",
    "qwerty",
    "admin",
    "letmein",
    "welcome",
    "monkey",
    "dragon",
    "baseball",
    "football",
    "superman",
    "batman",
    "trustno1",
    "iloveyou",
    "sunshine",
    "princess",
    "starwars",
    "master",
    "hello",
    "default",
    "changeme",
    "test",
    "demo",
    "dev",
    "prod",
    "example",
    "sample",
    "temporary",
    "temp",
    "key",
    "token",
    "pass",
    "pwd",
    "your",
    "here",
    "change",
    "production",
    "development",
    "local",
    "localhost",
    "app",
    "rag",
}

# 常见完整弱密钥（精确匹配，不区分大小写）
_WEAK_SECRET_EXACT: Set[str] = {
    "your-secret-key-here-change-in-production",
    "change_me_to_a_strong_random_key",
    "change-me-in-production",
    "change_me_in_production",
    "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "",
}


class SecretKeyValidationError(ValueError):
    """SECRET_KEY 校验失败异常。"""

    pass


def validate_secret_key(secret_key: str, in_docker: bool = True) -> None:
    """校验 SECRET_KEY 强度。

    规则：
    1. 非空；
    2. 长度不少于 32 字符；
    3. 不在弱密钥黑名单中；
    4. 字符种类不少于 3 种（大写、小写、数字、特殊字符）。

    Args:
        secret_key: 待校验的密钥。
        in_docker: 是否在 Docker 环境中运行。非 Docker 环境下仅做弱值提醒。

    Raises:
        SecretKeyValidationError: 校验失败时抛出。
    """
    if not secret_key:
        raise SecretKeyValidationError("SECRET_KEY 不能为空")

    normalized = secret_key.strip().lower()
    exact_normalized = secret_key.strip()

    if exact_normalized in _WEAK_SECRET_EXACT:
        raise SecretKeyValidationError("SECRET_KEY 使用了已知弱默认值")

    for keyword in _WEAK_SECRET_KEYWORDS:
        if keyword in normalized:
            raise SecretKeyValidationError(f"SECRET_KEY 包含弱关键字: {keyword}")

    if len(secret_key) < 32:
        raise SecretKeyValidationError(f"SECRET_KEY 长度必须不少于 32 字符，当前 {len(secret_key)} 字符")

    char_classes = 0
    if re.search(r"[a-z]", secret_key):
        char_classes += 1
    if re.search(r"[A-Z]", secret_key):
        char_classes += 1
    if re.search(r"[0-9]", secret_key):
        char_classes += 1
    if re.search(r"[^a-zA-Z0-9]", secret_key):
        char_classes += 1

    if char_classes < 3:
        raise SecretKeyValidationError("SECRET_KEY 必须同时包含至少 3 种字符类型（大写、小写、数字、特殊字符）")


def is_secret_key_strong(secret_key: str) -> bool:
    """判断 SECRET_KEY 是否足够强，不抛出异常。

    Args:
        secret_key: 待校验的密钥。

    Returns:
        bool: 是否通过强密钥校验。
    """
    try:
        validate_secret_key(secret_key)
        return True
    except SecretKeyValidationError:
        return False


class UnsafeUrlError(ValueError):
    """URL 存在安全风险异常。"""

    pass


# 云厂商元数据地址黑名单（大小写不敏感）
_METADATA_HOSTS: Set[str] = {
    "169.254.169.254",
    "metadata.google.internal",
    "metadata.oraclecloud.com",
    "169.254.170.2",
    "100.100.100.200",
}


# 域名保留后缀黑名单：本地/内网专用域名后缀，无需 DNS 解析直接拒绝
_RESERVED_HOST_SUFFIXES: tuple = (".localhost", ".local", ".internal", ".localdomain")


def validate_url_safe(url: str) -> None:
    """校验 URL 是否安全，防止 SSRF。

    规则：
    1. scheme 必须为 http 或 https；
    2. 禁止空主机；
    3. 禁止云厂商元数据地址与 localhost/保留后缀域名；
    4. 禁止私有 IP、回环地址、链路本地地址（含域名解析出的全部地址，
       防止攻击者域名解析到内网 IP 的 DNS 重绑定绕过）。

    注意：校验时刻解析与实际连接时刻存在 TOCTOU 窗口（攻击者可在两次
    解析间切换 DNS 记录）；彻底封死需将校验时解析的 IP 钉入连接层，
    当前按"解析-校验"闭环提供纵深防御。

    Args:
        url: 待校验的 URL。

    Raises:
        UnsafeUrlError: URL 存在 SSRF 风险时抛出。
    """
    if not url or not isinstance(url, str):
        raise UnsafeUrlError("URL 不能为空")

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise UnsafeUrlError(f"不允许的 URL scheme: {parsed.scheme}")

    hostname = parsed.hostname
    if not hostname:
        raise UnsafeUrlError("URL 缺少主机名")

    hostname_lower = hostname.lower()
    if hostname_lower in _METADATA_HOSTS:
        raise UnsafeUrlError("URL 指向云元数据服务，已被禁止")

    if hostname_lower == "localhost" or hostname_lower.endswith(_RESERVED_HOST_SUFFIXES):
        raise UnsafeUrlError(f"URL 指向本地/保留域名: {hostname_lower}")

    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        # 主机名不是 IP 字面量：DNS 解析后逐一校验结果地址。
        # 解析失败（gaierror）时放行——此时后续真实连接同样无法建立，
        # 不损失可用性；解析成功则任意一个受限地址即拒绝。
        try:
            addrinfos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        except (socket.gaierror, OSError):
            return
        for info in addrinfos:
            addr = info[4][0]
            try:
                resolved = ipaddress.ip_address(addr)
            except ValueError:
                continue
            if (
                resolved.is_private
                or resolved.is_loopback
                or resolved.is_link_local
                or resolved.is_multicast
                or resolved.is_reserved
            ):
                raise UnsafeUrlError(f"URL 解析到受限地址: {resolved}")
        return

    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
        raise UnsafeUrlError(f"URL 指向受限地址: {ip}")


def is_url_safe(url: str) -> bool:
    """判断 URL 是否安全，不抛出异常。

    Args:
        url: 待校验的 URL。

    Returns:
        bool: 是否通过安全校验。
    """
    try:
        validate_url_safe(url)
        return True
    except UnsafeUrlError:
        return False


def sanitize_url_for_display(url: str) -> Optional[str]:
    """过滤用于前端展示的 URL，仅保留 http/https。

    Args:
        url: 待过滤的 URL。

    Returns:
        str | None: 安全的 URL 或 None。
    """
    if not url or not isinstance(url, str):
        return None
    try:
        validate_url_safe(url)
        return url
    except UnsafeUrlError:
        return None
