"""
PostgreSQL连接测试
"""
import pytest

# 依赖真实 PostgreSQL（直接建立连接），默认跳过
pytestmark = pytest.mark.integration
import psycopg2

from src.config import settings


class TestPostgresConnection:
    """PostgreSQL连接测试类"""

    def test_postgres_connection_success(self):
        """测试PostgreSQL连接成功"""
        conn = psycopg2.connect(
            dbname=settings.database.POSTGRES_DB,
            user=settings.database.POSTGRES_USER,
            password=settings.database.POSTGRES_PASSWORD,
            host=settings.database.POSTGRES_HOST,
            port=settings.database.POSTGRES_PORT,
            connect_timeout=5
        )

        assert conn is not None
        assert conn.closed == 0

        conn.close()
        assert conn.closed == 1

    def test_postgres_connection_with_invalid_credentials(self):
        """测试使用无效凭据连接PostgreSQL"""
        with pytest.raises(psycopg2.OperationalError):
            psycopg2.connect(
                dbname="invalid_db",
                user="invalid_user",
                password="invalid_password",
                host=settings.database.POSTGRES_HOST,
                port=settings.database.POSTGRES_PORT,
                connect_timeout=5
            )
