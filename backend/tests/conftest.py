"""
pytest配置文件
"""

import asyncio
import os
import sys

import pytest

# 添加项目路径到Python路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Windows 下使用 SelectorEventLoop 替代 ProactorEventLoop，
# 避免 TestClient 跨测试复用事件循环与连接池时触发 "Event loop is closed" /
# "'NoneType' object has no attribute 'send'" 错误。
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


# 共享 TestClient 是否存活。存活期间跳过 engine.dispose：
# 此时 asyncpg 连接始终绑定在共享 TestClient 的事件循环上，跨循环 dispose
# 反而会污染连接池（dispose 在临时循环中执行会抛异常被吞，残留半关闭连接）。
_shared_client_active = False


@pytest.fixture(scope="session")
def integration_client():
    """进程级共享 TestClient（集成测试统一入口）。

    TestClient 自带独立事件循环线程，而 CacheService（Redis 客户端）、
    DB 引擎等进程级异步单例绑定在首次初始化它们的事件循环上；若每个测试
    新建 TestClient，后续测试的新循环复用这些单例就会触发
    "Event loop is closed" / 连接超时。故所有集成测试共享同一个 TestClient，
    让单例生命周期与单一事件循环保持一致。
    """
    global _shared_client_active
    from fastapi.testclient import TestClient
    from src.main import app

    with TestClient(app) as c:
        _shared_client_active = True
        yield c
    _shared_client_active = False


@pytest.fixture(autouse=True)
def _reset_async_engine():
    """在每个测试函数结束后释放异步数据库连接池。

    TestClient 每次创建独立的事件循环线程，asyncpg 连接会绑定到特定事件循环。
    若不在 teardown 时清空连接池，后续测试会复用绑定到已关闭事件循环的连接，
    导致 "cannot perform operation: another operation is in progress" 等错误。

    共享 TestClient 存活期间跳过（见 _shared_client_active 注释）。
    """
    yield
    if _shared_client_active:
        return
    try:
        from src.database import async_engine
        asyncio.run(async_engine.dispose())
    except Exception:
        pass


def pytest_addoption(parser):
    """注册自定义命令行选项。"""
    parser.addoption(
        "--run-e2e",
        action="store_true",
        default=False,
        help="运行标记为 e2e 的端到端测试（默认跳过，需外部服务）",
    )
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="运行标记为 integration 的集成测试（默认跳过，需 PostgreSQL/MinIO/Milvus 等真实服务）",
    )


def pytest_configure(config):
    """注册自定义 marker，避免 PytestUnknownMarkWarning。"""
    config.addinivalue_line(
        "markers",
        "integration: 集成测试，依赖真实外部服务（PostgreSQL/MinIO/Milvus/Redis），"
        "默认跳过，传入 --run-integration 运行",
    )


def pytest_collection_modifyitems(config, items):
    """根据 --run-e2e / --run-integration 选项决定是否跳过对应测试。

    未传入对应选项时，所有标记 e2e / integration 的测试自动跳过；
    传入后正常执行。
    """
    skip_e2e = pytest.mark.skip(reason="需要 --run-e2e 选项运行（依赖外部服务）")
    skip_integration = pytest.mark.skip(reason="需要 --run-integration 选项运行（依赖真实外部服务）")
    run_e2e = config.getoption("--run-e2e")
    run_integration = config.getoption("--run-integration")
    for item in items:
        if "e2e" in item.keywords and not run_e2e:
            item.add_marker(skip_e2e)
        if "integration" in item.keywords and not run_integration:
            item.add_marker(skip_integration)


@pytest.fixture(autouse=True)
def configure_encoding():
    """自动配置编码和环境变量的fixture"""
    # 设置PostgreSQL相关环境变量
    os.environ['PGCLIENTENCODING'] = 'UTF8'
    os.environ['LC_ALL'] = 'en_US.UTF-8'
    os.environ['LANG'] = 'en_US.UTF-8'

    yield