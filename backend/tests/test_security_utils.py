"""安全工具函数单元测试。"""

import pytest
from src.utils.security import (
    validate_secret_key,
    is_secret_key_strong,
    SecretKeyValidationError,
    validate_url_safe,
    is_url_safe,
    UnsafeUrlError,
)


class TestSecretKeyValidation:
    """SECRET_KEY 强度校验测试。"""

    def test_empty_secret_key_raises(self):
        with pytest.raises(SecretKeyValidationError):
            validate_secret_key("")

    def test_weak_default_secret_key_raises(self):
        with pytest.raises(SecretKeyValidationError):
            validate_secret_key("your-secret-key-here-change-in-production")

    def test_short_secret_key_raises(self):
        with pytest.raises(SecretKeyValidationError):
            validate_secret_key("Ab1!")

    def test_secret_key_with_weak_keyword_raises(self):
        with pytest.raises(SecretKeyValidationError):
            validate_secret_key("ThisIsAVeryLongPasswordWithNumbers123!")

    def test_secret_key_with_single_char_class_raises(self):
        with pytest.raises(SecretKeyValidationError):
            validate_secret_key("abcdefghijklmnopqrstuvwxyzabcdef")

    def test_strong_secret_key_passes(self):
        key = "A9#kL2$mPqR5@vXyZ1!bC3dE4fG5hJ6x"
        validate_secret_key(key)
        assert is_secret_key_strong(key) is True

    def test_is_secret_key_strong_returns_false_for_weak(self):
        assert is_secret_key_strong("weak") is False


class TestUrlValidation:
    """URL SSRF 防护测试。"""

    def test_http_url_is_safe(self):
        assert is_url_safe("http://example.com/path") is True

    def test_https_url_is_safe(self):
        assert is_url_safe("https://example.com/path") is True

    def test_javascript_url_is_unsafe(self):
        assert is_url_safe("javascript:alert(1)") is False

    def test_data_url_is_unsafe(self):
        assert is_url_safe("data:text/html,<script>alert(1)</script>") is False

    def test_private_ip_is_unsafe(self):
        assert is_url_safe("http://192.168.1.1/admin") is False

    def test_loopback_is_unsafe(self):
        assert is_url_safe("http://127.0.0.1/metadata") is False

    def test_metadata_address_is_unsafe(self):
        assert is_url_safe("http://169.254.169.254/latest/meta-data/") is False

    def test_empty_url_is_unsafe(self):
        assert is_url_safe("") is False

    def test_validate_url_safe_raises_on_unsafe(self):
        with pytest.raises(UnsafeUrlError):
            validate_url_safe("ftp://internal.server")
