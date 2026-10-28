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
