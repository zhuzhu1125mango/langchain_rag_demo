"""OPS-3 结构化日志上下文单测。

覆盖：上下文过滤器注入、text/JSON 格式、请求/阶段作用域复位、stage 装饰器
（async 函数 / async generator / 同步函数）、认证用户绑定与中间件贯通。
"""

import asyncio
import json
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.auth import CurrentUser, _bind_auth_user
from src.main import RequestTracingMiddleware
from src.utils import log_context
from src.utils.log_context import (
    ContextLogFilter,
    JsonLogFormatter,
    TEXT_LOG_FORMAT,
    bind_request_id,
    bind_stage,
    bind_user,
    build_formatter,
    current_context,
    log_stage,
    request_scope,
    reset,
    stage_scope,
)


def _record(msg="hello", **extra):
    record = logging.LogRecord("test", logging.INFO, __file__, 1, msg, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_context_filter_injects_fields():
    tokens = (bind_request_id("rid-1"), bind_user("u-1"), bind_stage("kb_retrieval"))
    try:
        record = _record()
        assert ContextLogFilter().filter(record) is True
        assert (record.request_id, record.user_id, record.stage) == (
            "rid-1",
            "u-1",
            "kb_retrieval",
        )
    finally:
        for token in reversed(tokens):
            reset(token)


def test_text_and_json_formatters():
    tokens = (bind_request_id("rid-2"), bind_user("u-2"), bind_stage("generation"))
    try:
        record = _record("答案已生成")
        ContextLogFilter().filter(record)
        text = build_formatter("text").format(record)
        assert "rid=rid-2 user=u-2 stage=generation" in text
        assert TEXT_LOG_FORMAT.startswith("%(asctime)s")

        payload = json.loads(JsonLogFormatter().format(record))
        assert payload["message"] == "答案已生成"
        assert payload["request_id"] == "rid-2"
        assert payload["user_id"] == "u-2"
        assert payload["stage"] == "generation"
        assert payload["level"] == "INFO"
        assert "ts" in payload
        # 未知格式回退 text
        assert "rid=rid-2" in build_formatter("unknown").format(record)
    finally:
        for token in reversed(tokens):
            reset(token)


def test_json_formatter_includes_exc_info():
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        import sys

        record = _record("失败")
        record.exc_info = sys.exc_info()
    ContextLogFilter().filter(record)
    payload = json.loads(JsonLogFormatter().format(record))
    assert "RuntimeError" in payload["exc_info"]


def test_request_scope_and_stage_scope_reset():
    with request_scope("rid-3"):
        assert current_context() == {"request_id": "rid-3", "user_id": "-", "stage": "-"}
        bind_user("u-3")
        with stage_scope("retrieval"):
            assert current_context()["stage"] == "retrieval"
        assert current_context()["stage"] == "-"
        assert current_context()["user_id"] == "u-3"
    assert current_context() == {"request_id": "-", "user_id": "-", "stage": "-"}


def test_reset_is_tolerant_for_stale_token():
    token = bind_stage("x")
    reset(token)
    reset(token)  # 二次复位不抛异常
    assert current_context()["stage"] == "-"


def test_log_stage_decorates_async_function():
    seen = {}

    @log_stage("intent")
    async def _work():
        seen.update(current_context())
        return "ok"

    assert asyncio.run(_work()) == "ok"
    assert seen["stage"] == "intent"
    assert current_context()["stage"] == "-"


def test_log_stage_decorates_async_generator():
    seen = []

    @log_stage("generation")
    async def _stream():
        for i in range(3):
            seen.append((i, current_context()["stage"]))
            yield i

    async def _consume():
        return [item async for item in _stream()]

    assert asyncio.run(_consume()) == [0, 1, 2]
    assert [stage for _, stage in seen] == ["generation", "generation", "generation"]
    assert current_context()["stage"] == "-"


def test_log_stage_decorates_sync_function_and_nesting():
    seen = {}

    @log_stage("outer")
    def _outer():
        @log_stage("inner")
        def _inner():
            seen["inner"] = current_context()["stage"]

        seen["outer"] = current_context()["stage"]
        _inner()
        seen["after_inner"] = current_context()["stage"]

    _outer()
    assert seen == {"outer": "outer", "inner": "inner", "after_inner": "outer"}


def test_bind_auth_user_updates_context():
    token = bind_user(None)
    try:
        user = _bind_auth_user(CurrentUser(user_id="u-9", is_authenticated=True))
        assert user.user_id == "u-9"
        assert current_context()["user_id"] == "u-9"
    finally:
        reset(token)


def test_middleware_injects_request_id_into_service_logs(caplog):
    app = FastAPI()
    app.add_middleware(RequestTracingMiddleware)

    @app.get("/ping")
    async def ping():
        logging.getLogger("rag_system").info("service-layer-log")
        return {"ok": True}

    with caplog.at_level(logging.INFO, logger="rag_system"):
        response = TestClient(app).get("/ping")

    assert response.status_code == 200
    records = [r for r in caplog.records if r.getMessage() == "service-layer-log"]
    assert records, "服务层日志应被捕获"
    assert records[0].request_id != "-"
    assert records[0].stage == "-"
