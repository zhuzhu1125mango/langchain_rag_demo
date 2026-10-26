"""模型 Provider API Key 加密（P2）。

生产建议配置独立的 ``MODEL_SECRET_ENC_KEY``（Fernet key）。未配置时回退
从 JWT ``SECRET_KEY`` 派生并告警；更换密钥后旧密文不可解密，需要重新录入。
"""

from __future__ import annotations

import base64
import hashlib
import logging
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from src.config import settings

logger = logging.getLogger("rag_system")

_warned_fallback = False


class ModelSecretCipherError(RuntimeError):
    """Provider Key 加密/解密失败。"""


def _fernet() -> Fernet:
    raw = getattr(settings.security, "MODEL_SECRET_ENC_KEY", "") or ""
    if raw:
        try:
            return Fernet(raw.encode("utf-8"))
        except Exception:
            # 允许用户填任意高熵字符串，派生为合法 Fernet key
            derived = base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest())
            return Fernet(derived)

    secret = getattr(settings.security, "SECRET_KEY", "") or ""
    if not secret:
        raise ModelSecretCipherError("MODEL_SECRET_ENC_KEY 与 SECRET_KEY 均未配置，无法保存 Provider API Key")
    global _warned_fallback
    if not _warned_fallback:
        logger.warning(
            "MODEL_SECRET_ENC_KEY 未配置，Provider Key 暂从 SECRET_KEY 派生；"
            "生产环境建议配置独立密钥并备份"
        )
        _warned_fallback = True
    derived = base64.urlsafe_b64encode(hashlib.sha256(f"model-provider:{secret}".encode("utf-8")).digest())
    return Fernet(derived)


def encrypt_secret(value: str) -> str:
    if not value:
        return ""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: Optional[str]) -> Optional[str]:
    if not ciphertext:
        return None
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError) as exc:
        raise ModelSecretCipherError("Provider API Key 解密失败（加密密钥可能已更换）") from exc


def secret_hint(value: Optional[str]) -> str:
    """返回安全展示提示（仅末四位）。"""
    if not value:
        return ""
    return f"****{value[-4:]}" if len(value) >= 4 else "****"
