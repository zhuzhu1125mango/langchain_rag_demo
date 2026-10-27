"""SEC P3 回归测试（2026-10-27）。

覆盖：
- SEC-16：CSP `connect-src` 收紧为同源 + HSTS 条件下发（静态配置守卫）
- SEC-17：评估 API 用户级限流/日配额 + 批量样本上限
- SEC-18：会话列表分页、批量删除上限、反馈/坏例列表上限
- SEC-19：URL 参数强制声明 + ToolGuard DNS 解析校验（async）
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.api import badcase_router, document_router, evaluation_router, feedback_router, session_router
from src.auth import get_current_user
from src.config import settings
from src.database import get_db
from src.services.guardrails.tool_guard import ToolGuard
from src.services.tools.tool_manager import BaseTool, ToolManager, ToolResult
from src.utils import quota as quota_module
from src.utils import rate_limit as rate_limit_module
from src.utils.security import UnsafeUrlError

NGINX_CONF = Path(__file__).resolve().parents[2] / "frontend" / "nginx.conf"


# ----------------------------------------------------------------------
# 测试脚手架
# ----------------------------------------------------------------------
class _FakeResult:
    def __init__(self, rows=None):
        self._rows = rows or []

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    @property
    def rowcount(self):
        return len(self._rows)


class _FakeDB:
    """最小 AsyncSession 桩：只覆盖本次涉及的调用。"""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.executed = []
        self.committed = False

    async def execute(self, statement, params=None):
        self.executed.append(statement)
        return _FakeResult(self.rows)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        pass


class _UrlTool(BaseTool):
    name = "url_tool"
    description = "URL 工具"
    risk_level = "external"
    parameters = {"type": "object", "properties": {"url": {"type": "string"}}}
    url_params = ("url",)

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(tool_name=self.name, output="ok")


class _UndeclaredUrlTool(_UrlTool):
    name = "undeclared_url_tool"
    url_params = ()


def _app(*routers_):
    app = FastAPI()
    for item in routers_:
        app.include_router(item)
    db = _FakeDB()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(user_id="u-1")
    return app, db


@pytest.fixture(autouse=True)
def _restore_quota_settings():
    saved = {
        "enabled": settings.security.QUOTA_ENFORCEMENT_ENABLED,
        "rpm": settings.security.EVAL_RATE_LIMIT_PER_MINUTE,
        "daily": settings.security.EVAL_DAILY_QUOTA,
        "samples": settings.security.EVAL_BATCH_MAX_SAMPLES,
        "max_ids": settings.security.BATCH_DELETE_MAX_IDS,
        "dns": settings.guardrail.GUARDRAIL_TOOL_DNS_CHECK_ENABLED,
        "guard": settings.guardrail.GUARDRAIL_ENABLED,
        "tool": settings.guardrail.GUARDRAIL_TOOL_ENABLED,
    }
    quota_module.reset_memory_quotas()
    rate_limit_module.reset_rate_limits()
    yield
    settings.security.QUOTA_ENFORCEMENT_ENABLED = saved["enabled"]
    settings.security.EVAL_RATE_LIMIT_PER_MINUTE = saved["rpm"]
    settings.security.EVAL_DAILY_QUOTA = saved["daily"]
    settings.security.EVAL_BATCH_MAX_SAMPLES = saved["samples"]
    settings.security.BATCH_DELETE_MAX_IDS = saved["max_ids"]
    settings.guardrail.GUARDRAIL_TOOL_DNS_CHECK_ENABLED = saved["dns"]
    settings.guardrail.GUARDRAIL_ENABLED = saved["guard"]
    settings.guardrail.GUARDRAIL_TOOL_ENABLED = saved["tool"]
    quota_module.reset_memory_quotas()
    rate_limit_module.reset_rate_limits()


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """配额与限流全部走进程内降级，测试不依赖 Redis。"""
    monkeypatch.setattr(quota_module, "get_redis_client_if_initialized", lambda: None)
    monkeypatch.setattr(rate_limit_module, "_redis_client", lambda: None)


# ----------------------------------------------------------------------
# SEC-16：CSP / HSTS
# ----------------------------------------------------------------------
def test_csp_connect_src_is_same_origin_only():
    content = NGINX_CONF.read_text(encoding="utf-8")
    assert "connect-src 'self'" in content
    assert "ws: wss:" not in content, "不应再放行任意外部 WebSocket 源"
    # 三处安全头块（server/index/assets）都应下发 HSTS 变量
    assert content.count("Strict-Transport-Security $hsts_header") == 3
    assert "map $http_x_forwarded_proto $hsts_header" in content


# ----------------------------------------------------------------------
# SEC-17：评估配额
# ----------------------------------------------------------------------
async def test_eval_quota_rate_limit_returns_429():
    settings.security.QUOTA_ENFORCEMENT_ENABLED = True
    settings.security.EVAL_RATE_LIMIT_PER_MINUTE = 1
    settings.security.EVAL_DAILY_QUOTA = 100

    await quota_module.consume_eval_quota("u-rpm", 1)
    with pytest.raises(HTTPException) as exc:
        await quota_module.consume_eval_quota("u-rpm", 1)
    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"] == "60"


async def test_eval_quota_daily_units():
    settings.security.QUOTA_ENFORCEMENT_ENABLED = True
    settings.security.EVAL_RATE_LIMIT_PER_MINUTE = 0
    settings.security.EVAL_DAILY_QUOTA = 2

    await quota_module.consume_eval_quota("u-daily", 2)
    with pytest.raises(HTTPException) as exc:
        await quota_module.consume_eval_quota("u-daily", 1)
    assert exc.value.status_code == 429
    assert "评估每日" in exc.value.detail


async def test_eval_quota_disabled_is_noop():
    settings.security.QUOTA_ENFORCEMENT_ENABLED = False
    settings.security.EVAL_RATE_LIMIT_PER_MINUTE = 1
    for _ in range(3):
        await quota_module.consume_eval_quota("u-off", 1)


def test_eval_batch_size_limit_returns_400():
    settings.security.EVAL_BATCH_MAX_SAMPLES = 2
    app, _ = _app(evaluation_router)
    client = TestClient(app)

    ok = client.post("/evaluate/retrieval/batch", json={"samples": [{}, {}]})
    assert ok.status_code == 200

    too_many = client.post("/evaluate/retrieval/batch", json={"samples": [{}, {}, {}]})
    assert too_many.status_code == 400
    assert "最多允许 2" in too_many.json()["detail"]


def test_eval_generation_batch_size_limit_returns_400():
    settings.security.EVAL_BATCH_MAX_SAMPLES = 1
    app, _ = _app(evaluation_router)
    client = TestClient(app)
    response = client.post("/evaluate/generation/batch", json={"samples": [{}, {}]})
    assert response.status_code == 400


# ----------------------------------------------------------------------
# SEC-18：分页与批量上限
# ----------------------------------------------------------------------
def test_session_list_pagination_params():
    settings.security.BATCH_DELETE_MAX_IDS = 200
    app, db = _app(session_router)
    client = TestClient(app)

    response = client.get("/sessions/", params={"skip": 5, "limit": 10})
    assert response.status_code == 200
    assert db.executed, "应执行一次分页查询"

    assert client.get("/sessions/", params={"limit": 501}).status_code == 422
    assert client.get("/sessions/", params={"skip": -1}).status_code == 422


def test_session_batch_delete_cap():
    settings.security.BATCH_DELETE_MAX_IDS = 1
    app, db = _app(session_router)
    client = TestClient(app)
    response = client.post(
        "/sessions/batch-delete",
        json={"ids": ["11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"]},
    )
    assert response.status_code == 400
    assert "最多允许 1" in response.json()["detail"]
    assert db.committed is False


def test_document_batch_delete_cap():
    settings.security.BATCH_DELETE_MAX_IDS = 1
    app, _ = _app(document_router)
    client = TestClient(app)
    response = client.post(
        "/documents/batch/delete",
        json={"ids": ["11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"]},
    )
    assert response.status_code == 400
    assert "最多允许 1" in response.json()["detail"]


def test_feedback_and_badcase_limit_cap():
    settings.security.BATCH_DELETE_MAX_IDS = 200
    app, _ = _app(feedback_router, badcase_router)
    client = TestClient(app)
    assert client.get("/feedback/", params={"limit": 201}).status_code == 422
    assert client.get("/badcases/", params={"limit": 201}).status_code == 422


# ----------------------------------------------------------------------
# SEC-19：URL 参数声明 + DNS 校验
# ----------------------------------------------------------------------
def test_tool_registration_requires_url_param_declaration():
    with pytest.raises(ValueError, match="未声明 URL 参数"):
        ToolManager().register_tool(_UndeclaredUrlTool())


def test_tool_registration_accepts_declared_url_param():
    manager = ToolManager()
    manager.register_tool(_UrlTool())
    assert manager.registry.get("url_tool") is not None


def test_fetch_webpage_declares_url_param():
    from src.services.tools.plugins.fetch_webpage_tool import FetchWebpageTool

    assert "url" in FetchWebpageTool.url_params


async def test_tool_guard_async_blocks_unresolvable_or_private_domain(monkeypatch):
    def _reject(url):
        raise UnsafeUrlError("解析到内网")

    monkeypatch.setattr("src.services.guardrails.tool_guard.validate_url_safe", _reject)
    hit = await ToolGuard().check_async(_UrlTool(), {"url": "https://evil.example/x"})
    assert hit is not None
    assert hit.rule == "dns_unresolved_or_private"


async def test_tool_guard_async_allows_safe_domain(monkeypatch):
    monkeypatch.setattr("src.services.guardrails.tool_guard.validate_url_safe", lambda url: None)
    assert await ToolGuard().check_async(_UrlTool(), {"url": "https://example.com/docs"}) is None


async def test_tool_guard_dns_check_can_be_disabled(monkeypatch):
    def _reject(url):
        raise UnsafeUrlError("解析到内网")

    monkeypatch.setattr("src.services.guardrails.tool_guard.validate_url_safe", _reject)
    settings.guardrail.GUARDRAIL_TOOL_DNS_CHECK_ENABLED = False
    assert await ToolGuard().check_async(_UrlTool(), {"url": "https://evil.example/x"}) is None


async def test_tool_manager_blocks_dns_unsafe_url(monkeypatch):
    def _reject(url):
        raise UnsafeUrlError("解析到内网")

    monkeypatch.setattr("src.services.guardrails.tool_guard.validate_url_safe", _reject)
    manager = ToolManager()
    manager.register_tool(_UrlTool())
    result = await manager.execute("url_tool", url="https://evil.example/x")
    assert result.success is False
    assert "安全护栏拦截" in (result.error or "")


async def test_tool_manager_allows_dns_safe_url(monkeypatch):
    monkeypatch.setattr("src.services.guardrails.tool_guard.validate_url_safe", lambda url: None)
    manager = ToolManager()
    manager.register_tool(_UrlTool())
    result = await manager.execute("url_tool", url="https://example.com/x")
    assert result.success is True
