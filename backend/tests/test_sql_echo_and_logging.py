"""
P1-3 回归测试：SQL echo 环境区分 + document.py 模块级 logger 清理（审查项 #22）。

验证：
1. DatabaseSettings.SQL_ECHO 默认 False（生产安全默认），engine echo 跟随配置，
   环境变量可覆盖（开发排错可开）；
2. document.py 导入后不再篡改全局 "rag_system" logger
   （模块级 setLevel(DEBUG)/addHandler 曾导致业务日志重复输出）。
"""

import logging
from pathlib import Path

import src.database as database  # noqa: F401  引擎随模块导入创建
from src.config import DatabaseSettings, settings

DOCUMENT_API = (
    Path(__file__).resolve().parent.parent / "src" / "api" / "document.py"
)


class TestSqlEchoConfig:
    """SQL_ECHO 配置项与引擎行为。"""

    def test_default_is_off(self):
        """SQL_ECHO 默认关闭。"""
        assert DatabaseSettings().SQL_ECHO is False

    def test_engine_echo_follows_settings(self):
        """async_engine 的 echo 与配置一致（不再硬编码 True）。"""
        assert database.async_engine.echo == settings.database.SQL_ECHO

    def test_env_override_enabled(self, monkeypatch):
        """环境变量 SQL_ECHO=true 可覆盖默认（开发打开）。"""
        monkeypatch.setenv("SQL_ECHO", "true")
        assert DatabaseSettings().SQL_ECHO is True

    def test_env_override_disabled(self, monkeypatch):
        """环境变量 SQL_ECHO=false 显式关闭。"""
        monkeypatch.setenv("SQL_ECHO", "false")
        assert DatabaseSettings().SQL_ECHO is False


class TestDocumentLoggerSideEffects:
    """document.py 模块级日志副作用清理。"""

    def test_source_has_no_module_level_logging_side_effects(self):
        """源码不得包含模块级 setLevel/addHandler。"""
        source = DOCUMENT_API.read_text(encoding="utf-8")
        assert "addHandler" not in source
        assert "setLevel" not in source

    def test_global_logger_has_no_extra_handlers(self):
        """导入 document 模块不得为全局 "rag_system" logger 追加 handler。

        pytest 进程内未执行 main.py 的 basicConfig，"rag_system" 作为
        子 logger 自身不应持有任何 handler（输出统一交给 root 配置）。
        """
        import src.api.document  # noqa: F401

        assert logging.getLogger("rag_system").handlers == []

    def test_global_logger_level_not_forced_to_debug(self):
        """模块导入不得将全局 logger 级别强制为 DEBUG。"""
        import src.api.document  # noqa: F401

        assert logging.getLogger("rag_system").level != logging.DEBUG
