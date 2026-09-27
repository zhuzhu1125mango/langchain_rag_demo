"""Docker Secrets 注入（W2-11）与 /metrics 鉴权（W2-12）测试。"""

from pathlib import Path

import pytest
from starlette.requests import Request as StarletteRequest

from src.config import settings, DatabaseSettings
from src.main import get_metrics_endpoint


# ---------------------------------------------------------------------------
# W2-11: pydantic-settings secrets_dir 注入（容器内 /run/secrets 场景）
# ---------------------------------------------------------------------------


class TestSecretsDirInjection:
    """secrets_dir 按字段名读取文件并以最高优先级覆盖默认值。"""

    def test_secret_file_overrides_field(self, tmp_path):
        (tmp_path / "POSTGRES_PASSWORD").write_text("s3cret-from-file")
        db = DatabaseSettings(_secrets_dir=str(tmp_path))
        assert db.POSTGRES_PASSWORD == "s3cret-from-file"

    def test_fields_without_secret_file_keep_defaults(self, tmp_path):
        (tmp_path / "POSTGRES_PASSWORD").write_text("s3cret-from-file")
        db = DatabaseSettings(_secrets_dir=str(tmp_path))
        baseline = DatabaseSettings()
        # 未提供 secret 文件的字段回退默认/env 文件值，与无 secrets_dir 的基线一致
        assert db.POSTGRES_HOST == baseline.POSTGRES_HOST
        assert db.POSTGRES_PORT == baseline.POSTGRES_PORT

    def test_unrelated_secret_files_are_ignored(self, tmp_path):
        (tmp_path / "TOTALLY_UNRELATED").write_text("whatever")
        db = DatabaseSettings(_secrets_dir=str(tmp_path))
        assert db.POSTGRES_PASSWORD == DatabaseSettings().POSTGRES_PASSWORD

    def test_secrets_dir_disabled_outside_container(self):
        """本地裸跑/CI 无 /run/secrets：SECRETS_DIR 为 None，行为与显式空目录一致。"""
        from src import config

        if config.SECRETS_DIR is None:
            baseline = DatabaseSettings()
            disabled = DatabaseSettings(_secrets_dir=str(Path("/nonexistent-secrets")))
            assert disabled.POSTGRES_PASSWORD == baseline.POSTGRES_PASSWORD
            assert disabled.POSTGRES_HOST == baseline.POSTGRES_HOST


# ---------------------------------------------------------------------------
# W2-12: /metrics Bearer 鉴权
# ---------------------------------------------------------------------------


def _request_with_auth(value: str | None) -> StarletteRequest:
    headers = []
    if value is not None:
        headers.append((b"authorization", value.encode()))
    return StarletteRequest({"type": "http", "headers": headers})


class TestMetricsAuth:
    def test_no_token_configured_allows_anonymous(self, monkeypatch):
        monkeypatch.setattr(settings.security, "METRICS_TOKEN", "")
        resp = get_metrics_endpoint(_request_with_auth(None))
        assert resp.status_code == 200

    def test_wrong_token_rejected(self, monkeypatch):
        monkeypatch.setattr(settings.security, "METRICS_TOKEN", "tok-123")
        with pytest.raises(Exception) as exc:
            get_metrics_endpoint(_request_with_auth("Bearer wrong"))
        assert getattr(exc.value, "status_code", None) == 401

    def test_missing_header_rejected(self, monkeypatch):
        monkeypatch.setattr(settings.security, "METRICS_TOKEN", "tok-123")
        with pytest.raises(Exception) as exc:
            get_metrics_endpoint(_request_with_auth(None))
        assert getattr(exc.value, "status_code", None) == 401

    def test_correct_token_allowed(self, monkeypatch):
        monkeypatch.setattr(settings.security, "METRICS_TOKEN", "tok-123")
        resp = get_metrics_endpoint(_request_with_auth("Bearer tok-123"))
        assert resp.status_code == 200

    def test_non_bearer_scheme_rejected(self, monkeypatch):
        monkeypatch.setattr(settings.security, "METRICS_TOKEN", "tok-123")
        with pytest.raises(Exception) as exc:
            get_metrics_endpoint(_request_with_auth("Basic tok-123"))
        assert getattr(exc.value, "status_code", None) == 401
