"""启动时的环境判据打印与危险组合告警（#33）。

为什么值得单独测：`IS_PRODUCTION` 不是配置项，是推导出来的（APP_ENV 优先，
否则看 IN_DOCKER），一份 .env 换台机器可能得出相反结论。这行日志是出问题时
第一句要看的，写错了会把人带偏，所以把「怎么判的」和「判成什么」都钉住。
"""

import logging

import pytest

import src.main as main_module
from src.config import settings


@pytest.fixture
def prod_env(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENV", "prod")
    monkeypatch.setattr(settings, "IN_DOCKER", True)
    return settings


class TestRuntimeEnvSummary:
    def test_summary_prints_criteria_and_conclusion(self, prod_env):
        text = main_module._runtime_env_summary()

        assert "APP_ENV=prod" in text
        assert "IN_DOCKER=True" in text
        assert "IS_PRODUCTION=True" in text, "结论必须显式打出来，不能让人自己推"

    def test_summary_never_leaks_secret_values(self, prod_env, monkeypatch):
        monkeypatch.setattr(settings.security, "ADMIN_KEY", "super-secret-key-value")
        monkeypatch.setattr(settings.security, "API_KEY", "another-secret")
        monkeypatch.setattr(settings.security, "METRICS_TOKEN", "metrics-secret")

        text = main_module._runtime_env_summary()

        assert "super-secret-key-value" not in text
        assert "another-secret" not in text
        assert "metrics-secret" not in text
        assert text.count("set") == 3, "只说明有没有配，不说明配了什么"

    def test_unset_marks_unset(self, prod_env, monkeypatch):
        monkeypatch.setattr(settings.security, "ADMIN_KEY", None)
        assert "ADMIN_KEY=unset" in main_module._runtime_env_summary()


class TestDangerousCombinations:
    def test_prod_without_admin_key_warns(self, prod_env, monkeypatch):
        monkeypatch.setattr(settings.security, "ADMIN_KEY", None)
        monkeypatch.setattr(settings.security, "METRICS_TOKEN", "t")

        warnings = main_module._dangerous_env_warnings()

        assert any("ADMIN_KEY" in w for w in warnings)

    def test_prod_without_metrics_token_warns(self, prod_env, monkeypatch):
        monkeypatch.setattr(settings.security, "ADMIN_KEY", "k")
        monkeypatch.setattr(settings.security, "METRICS_TOKEN", "")

        warnings = main_module._dangerous_env_warnings()

        assert any("METRICS_TOKEN" in w for w in warnings)

    def test_prod_fully_configured_is_quiet(self, prod_env, monkeypatch):
        monkeypatch.setattr(settings.security, "ADMIN_KEY", "k")
        monkeypatch.setattr(settings.security, "METRICS_TOKEN", "t")

        assert main_module._dangerous_env_warnings() == []

    def test_dev_is_quiet_even_without_keys(self, monkeypatch):
        """开发模式不配这些是正常的，告警刷屏只会让人忽略真正的告警。"""
        monkeypatch.setattr(settings, "APP_ENV", "dev")
        monkeypatch.setattr(settings, "IN_DOCKER", False)
        monkeypatch.setattr(settings.security, "ADMIN_KEY", None)
        monkeypatch.setattr(settings.security, "METRICS_TOKEN", "")

        assert main_module._dangerous_env_warnings() == []


class TestLogging:
    def test_logs_info_line_with_env(self, prod_env, monkeypatch, caplog):
        monkeypatch.setattr(settings.security, "ADMIN_KEY", "k")
        monkeypatch.setattr(settings.security, "METRICS_TOKEN", "t")

        with caplog.at_level(logging.INFO, logger="rag_system"):
            main_module._log_runtime_environment()

        messages = [r.getMessage() for r in caplog.records]
        assert any("运行环境:" in m and "IS_PRODUCTION=True" in m for m in messages)

    def test_warning_uses_stable_prefix(self, prod_env, monkeypatch, caplog):
        """前缀 `[env-check]` 要稳定：告警规则靠 grep 它来聚合。"""
        monkeypatch.setattr(settings.security, "ADMIN_KEY", None)
        monkeypatch.setattr(settings.security, "METRICS_TOKEN", "t")

        with caplog.at_level(logging.WARNING, logger="rag_system"):
            main_module._log_runtime_environment()

        assert any("[env-check]" in r.getMessage() for r in caplog.records)
