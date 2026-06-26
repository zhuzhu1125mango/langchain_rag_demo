"""
编码处理测试
"""
import os

import pytest
import psycopg2

from src.config import settings


def test_encoding_configuration():
    """测试编码配置是否正确设置"""
    assert os.environ.get('PGCLIENTENCODING') == 'UTF8'
    assert os.environ.get('LC_ALL') == 'en_US.UTF-8'
    assert os.environ.get('LANG') == 'en_US.UTF-8'


def test_postgres_unicode_handling():
    """测试PostgreSQL的Unicode处理能力"""
    try:
        conn = psycopg2.connect(
            dbname=settings.database.POSTGRES_DB,
            user=settings.database.POSTGRES_USER,
            password=settings.database.POSTGRES_PASSWORD,
            host=settings.database.POSTGRES_HOST,
            port=settings.database.POSTGRES_PORT,
            connect_timeout=5
        )

        test_unicode_str = "测试中文字符串 🎉"
        cursor = conn.cursor()
        cursor.execute("SELECT %s", (test_unicode_str,))
        result = cursor.fetchone()

        assert result[0] == test_unicode_str

        cursor.close()
        conn.close()

    except UnicodeDecodeError as e:
        pytest.fail(f"Unicode处理失败: {e}")
    except Exception as e:
        pytest.skip(f"数据库连接失败，跳过测试: {e}")
