"""
pytest配置文件
"""

import os
import sys

import pytest

# 添加项目路径到Python路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def configure_encoding():
    """自动配置编码和环境变量的fixture"""
    # 设置PostgreSQL相关环境变量
    os.environ['PGCLIENTENCODING'] = 'UTF8'
    os.environ['LC_ALL'] = 'en_US.UTF-8'
    os.environ['LANG'] = 'en_US.UTF-8'

    yield