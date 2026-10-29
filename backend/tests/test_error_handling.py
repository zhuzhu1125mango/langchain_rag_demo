"""CODE-2：异常分级与统一记录入口的单元测试。"""

import asyncio
import logging

import pytest
from fastapi import HTTPException

from src.exceptions import ValidationException
from src.utils.error_handling import (
    ErrorSeverity,
    classify_exception,
    format_context,
    is_recoverable,
    log_exception,
)


class _FakeHttpxError(Exception):
    """模拟 httpx 抛出的传输层异常（按模块前缀识别）。"""


_FakeHttpxError.__module__ = "httpx"
_FakeHttpxError.__name__ = "ConnectError"


class _FakeMilvusError(Exception):
    """模拟 MilvusException：类名不含特征，靠消息里的 rate limit 识别。"""


class TestClassify:
    @pytest.mark.parametrize(
        "exc",
        [
            TypeError("bad type"),
            AttributeError("no attr"),
            KeyError("missing"),
            IndexError("oob"),
            AssertionError("boom"),
            NameError("nope"),
            ZeroDivisionError("div"),
            NotImplementedError("todo"),
        ],
    )
    def test_programming_errors(self, exc):
        assert classify_exception(exc) is ErrorSeverity.PROGRAMMING

    def test_invalid_argument_is_programming(self):
        class InvalidArgumentError(Exception):
            pass

        assert classify_exception(InvalidArgumentError("bad arg")) is ErrorSeverity.PROGRAMMING

    @pytest.mark.parametrize(
        "exc",
        [
            TimeoutError("slow"),
            asyncio.TimeoutError("slow"),
            ConnectionResetError("reset"),
            ConnectionRefusedError("refused"),
            BrokenPipeError("pipe"),
        ],
    )
    def test_recoverable_builtin_errors(self, exc):
        assert classify_exception(exc) is ErrorSeverity.RECOVERABLE

    def test_recoverable_by_module_prefix(self):
        assert classify_exception(_FakeHttpxError("connect failed")) is ErrorSeverity.RECOVERABLE

    def test_recoverable_by_message_signal(self):
        assert classify_exception(_FakeMilvusError("request rejected: rate limit exceeded")) is (
            ErrorSeverity.RECOVERABLE
        )

    def test_business_validation_is_reportable(self):
        """业务校验失败不是「可恢复」，也不该被当成编程错误自动重试。"""
        assert classify_exception(ValidationException("bad input")) is ErrorSeverity.REPORTABLE

    def test_http_exception_is_reportable(self):
        assert classify_exception(HTTPException(status_code=404)) is ErrorSeverity.REPORTABLE

    def test_unknown_error_defaults_to_reportable(self):
        assert classify_exception(RuntimeError("unexpected")) is ErrorSeverity.REPORTABLE

    def test_message_mentioning_connection_does_not_override_programming(self):
        """编程错误即便文案里带 connection 也应保持 PROGRAMMING（类型优先）。"""
        assert classify_exception(KeyError("connection refused")) is ErrorSeverity.PROGRAMMING

    def test_is_recoverable_helper(self):
        assert is_recoverable(TimeoutError()) is True
        assert is_recoverable(RuntimeError()) is False


class TestLogException:
    def test_recoverable_logs_warning_without_stack(self, caplog):
        logger = logging.getLogger("test.error_handling.recoverable")
        with caplog.at_level(logging.DEBUG, logger=logger.name):
            severity = log_exception(
                logger, TimeoutError("upstream slow"), action="kb.search", kb_id="kb-1"
            )
        assert severity is ErrorSeverity.RECOVERABLE
        record = caplog.records[-1]
        assert record.levelno == logging.WARNING
        assert record.exc_info is None, "可恢复异常不应刷堆栈"
        assert "action=kb.search" in record.getMessage()
        assert "severity=recoverable" in record.getMessage()
        assert "error=TimeoutError" in record.getMessage()
        assert "kb_id=kb-1" in record.getMessage()

    def test_reportable_logs_error_with_stack(self, caplog):
        logger = logging.getLogger("test.error_handling.reportable")
        with caplog.at_level(logging.DEBUG, logger=logger.name):
            severity = log_exception(logger, RuntimeError("boom"), action="document.classify")
        assert severity is ErrorSeverity.REPORTABLE
        record = caplog.records[-1]
        assert record.levelno == logging.ERROR
        assert record.exc_info is not None

    def test_programming_logs_error_with_stack(self, caplog):
        logger = logging.getLogger("test.error_handling.programming")
        with caplog.at_level(logging.DEBUG, logger=logger.name):
            severity = log_exception(logger, TypeError("oops"), action="document.quality")
        assert severity is ErrorSeverity.PROGRAMMING
        record = caplog.records[-1]
        assert record.levelno == logging.ERROR
        assert "severity=programming" in record.getMessage()

    def test_severity_override_wins(self, caplog):
        """调用方显式分级优先于自动判定（如确知某 SDK 错误可降级）。"""
        logger = logging.getLogger("test.error_handling.override")
        with caplog.at_level(logging.DEBUG, logger=logger.name):
            severity = log_exception(
                logger,
                RuntimeError("weird"),
                action="kb.upload",
                severity=ErrorSeverity.RECOVERABLE,
            )
        assert severity is ErrorSeverity.RECOVERABLE
        assert caplog.records[-1].levelno == logging.WARNING

    def test_context_rendering_skips_none(self):
        text = format_context(
            "document.delete", ErrorSeverity.REPORTABLE, RuntimeError("x"), doc_id="d-1", kb_id=None
        )
        assert "doc_id=d-1" in text
        assert "kb_id" not in text
        assert text.startswith("action=document.delete")


class TestFailMapping:
    """CODE-2 边界映射：业务异常→4xx 且不打 error 日志；未预期→500 且记录。"""

    def test_not_found_maps_404(self):
        from src.api.admin_models import _fail
        from src.services.model_config_service import ModelConfigNotFound

        resp = _fail(ModelConfigNotFound("provider 不存在"), "admin_models.test")
        assert resp.status_code == 404
        assert "provider 不存在" in str(resp.detail)

    def test_conflict_maps_409(self):
        from src.api.admin_models import _fail
        from src.services.model_config_service import ModelConfigConflict

        assert _fail(ModelConfigConflict("已存在"), "admin_models.test").status_code == 409

    def test_business_validation_maps_400(self):
        from src.api.admin_models import _fail
        from src.services.model_config_service import ModelConfigError

        assert _fail(ModelConfigError("参数非法"), "admin_models.test").status_code == 400

    def test_unexpected_maps_500_and_logs(self, caplog):
        import logging as _logging

        from src.api.admin_models import _fail

        logger = _logging.getLogger("rag_system")
        with caplog.at_level(_logging.DEBUG, logger="rag_system"):
            resp = _fail(RuntimeError("db down"), "admin_models.registry.apply")

        assert resp.status_code == 500
        assert "db down" not in str(resp.detail), "对外文案不应泄漏内部异常"
        record = caplog.records[-1]
        assert record.levelno == _logging.ERROR
        assert "action=admin_models.registry.apply" in record.getMessage()
        assert "severity=reportable" in record.getMessage()


class TestApiFailHelpers:
    """CODE-2 第三批：experiment / learning 接口兜底统一为"记录 + 500"，不泄漏内部细节。"""

    @pytest.mark.parametrize(
        "module,action",
        [
            ("src.api.experiment", "experiment.analyze"),
            ("src.api.learning", "learning.trigger"),
        ],
    )
    def test_unexpected_maps_500_and_records_action(self, module, action, caplog):
        import importlib
        import logging as _logging

        _fail = importlib.import_module(module)._fail
        logger = _logging.getLogger("rag_system")
        with caplog.at_level(_logging.DEBUG, logger="rag_system"):
            resp = _fail(ValueError("内部细节 db=rag"), action)

        assert resp.status_code == 500
        assert "内部细节" not in str(resp.detail)
        record = caplog.records[-1]
        assert record.levelno == _logging.ERROR
        assert f"action={action}" in record.getMessage()
        assert "severity=reportable" in record.getMessage()

    def test_learning_stats_degrades_without_silence(self, caplog):
        """误分类统计失败应保留主统计，但必须留下可恢复级日志（不能静默归零）。"""
        logger = logging.getLogger("rag_system")
        with caplog.at_level(logging.DEBUG, logger="rag_system"):
            log_exception(
                logger, RuntimeError("rule learner down"),
                action="learning.stats.misclassification",
                severity=ErrorSeverity.RECOVERABLE,
                message="误分类样本统计失败，计数降级为 0",
            )
        record = caplog.records[-1]
        assert record.levelno == logging.WARNING
        assert "action=learning.stats.misclassification" in record.getMessage()
