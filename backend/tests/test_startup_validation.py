"""
启动安全校验单元测试。

覆盖 IS_PRODUCTION 判定路径与生产模式下的凭据强制校验逻辑，
确保非 Docker 的生产部署同样受保护（回归：校验此前仅限 IN_DOCKER）。

注意：pymilvus 在 import 时会执行 load_dotenv()，把根目录 .env 灌入
os.environ，而 pydantic-settings 中环境变量优先于代码默认值。因此
所有断言均通过显式传参或 model_fields 默认值进行，不依赖构造默认值。
"""

import pytest

from src.config import DatabaseSettings, MinIOSettings, SecuritySettings, Settings
from src.utils.security import SecretKeyValidationError, validate_secret_key

STRONG_KEY = "Tq7#mZv2$Lp9wKn3@Rf5xJd8&Hs2bYc6"


class TestIsProduction:
    """Settings.IS_PRODUCTION 判定路径。"""

    def _settings(self, **kwargs) -> Settings:
        # 显式传入全部字段，隔离本机 .env / 环境变量（pymilvus load_dotenv 污染）
        kwargs.setdefault(
            "security", SecuritySettings(SECRET_KEY=STRONG_KEY)
        )
        return Settings(_env_file=None, **kwargs)

    def test_docker_implies_production(self):
        """Docker 部署（IN_DOCKER=true）默认按生产标准校验，向后兼容。"""
        s = self._settings(IN_DOCKER=True)
        assert s.IS_PRODUCTION is True

    def test_dev_default_is_not_production(self):
        s = self._settings(IN_DOCKER=False)
        assert s.IS_PRODUCTION is False

    def test_app_env_production_without_docker(self):
        """非 Docker 的生产部署：显式 APP_ENV=production。"""
        s = self._settings(IN_DOCKER=False, APP_ENV="production")
        assert s.IS_PRODUCTION is True

    def test_app_env_dev_overrides_docker(self):
        """显式 APP_ENV=dev 时优先于 IN_DOCKER（本地容器调试场景）。"""
        s = self._settings(IN_DOCKER=True, APP_ENV="dev")
        assert s.IS_PRODUCTION is False

    def test_app_env_invalid_value_not_production(self):
        """未知 APP_ENV 值不视为生产（仅显式 'production' 触发强校验）。"""
        s = self._settings(IN_DOCKER=False, APP_ENV="staging")
        assert s.IS_PRODUCTION is False

    def test_no_default_credentials(self):
        """回归：config.py 不再内置弱凭据默认值（直接断言类字段默认值）。"""
        assert SecuritySettings.model_fields["SECRET_KEY"].default == ""
        assert MinIOSettings.model_fields["MINIO_ACCESS_KEY"].default == ""
        assert MinIOSettings.model_fields["MINIO_SECRET_KEY"].default == ""


class TestProductionCredentialRules:
    """生产模式凭据校验规则（与 main.py lifespan 中逻辑对齐）。"""

    @staticmethod
    def _check_production(s: Settings):
        """复现 lifespan 的校验逻辑，供断言使用。"""
        errors = []
        try:
            validate_secret_key(s.security.SECRET_KEY, in_docker=True)
        except SecretKeyValidationError as e:
            errors.append(f"SECRET_KEY: {e}")
        if not s.database.POSTGRES_PASSWORD:
            errors.append("POSTGRES_PASSWORD")
        if not s.minio.MINIO_ACCESS_KEY:
            errors.append("MINIO_ACCESS_KEY")
        if not s.minio.MINIO_SECRET_KEY:
            errors.append("MINIO_SECRET_KEY")
        return errors

    def _production_settings(self, **overrides) -> Settings:
        """构造全部凭据置空的生产配置，overrides 按需提供子配置实例。"""
        base = dict(
            security=SecuritySettings(SECRET_KEY=""),
            database=DatabaseSettings(POSTGRES_PASSWORD=""),
            minio=MinIOSettings(MINIO_ACCESS_KEY="", MINIO_SECRET_KEY=""),
            IN_DOCKER=False,
            APP_ENV="production",
        )
        base.update(overrides)
        return Settings(_env_file=None, **base)

    def test_weak_secret_key_rejected(self):
        s = self._production_settings(
            security=SecuritySettings(SECRET_KEY="your-secret-key-here-change-in-production")
        )
        errors = self._check_production(s)
        assert any("SECRET_KEY" in e for e in errors)

    def test_empty_secret_key_rejected(self):
        s = self._production_settings()
        errors = self._check_production(s)
        assert any("SECRET_KEY" in e for e in errors)

    def test_empty_minio_credentials_rejected(self):
        """回归：MinIO 默认值已移除，生产模式空凭据必须被拦截。"""
        s = self._production_settings(security=SecuritySettings(SECRET_KEY=STRONG_KEY))
        errors = self._check_production(s)
        assert "MINIO_ACCESS_KEY" in errors
        assert "MINIO_SECRET_KEY" in errors
        assert "POSTGRES_PASSWORD" in errors

    def test_strong_config_passes(self):
        s = self._production_settings(
            security=SecuritySettings(SECRET_KEY=STRONG_KEY),
            database=DatabaseSettings(POSTGRES_PASSWORD="strong-db-pass-123"),
            minio=MinIOSettings(MINIO_ACCESS_KEY="prod-access", MINIO_SECRET_KEY="prod-secret-123456"),
        )
        assert self._check_production(s) == []
