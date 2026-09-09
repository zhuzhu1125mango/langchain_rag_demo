"""Wiki 页面 API 单元测试（Phase 2，独立子应用 + 依赖覆盖，无外部服务）。

覆盖：页面列表 / 正文预览 / 全量重编译（owner 校验、404/400、
rebuild 后台任务提交）。
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.wiki import router
from src.auth import CurrentUser, get_current_user
from src.database import get_db

USER_ID = "u-1"


class FakeScalars:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None


class FakeResult:
    def __init__(self, items):
        self._scalars = FakeScalars(items)

    def scalars(self):
        return self._scalars

    def scalar_one_or_none(self):
        return self._scalars.scalar_one_or_none()


class FakeDB:
    """按调用次序弹出 select 结果的假会话。"""

    def __init__(self, select_queue):
        self.select_queue = list(select_queue)

    async def execute(self, stmt):
        return FakeResult(self.select_queue.pop(0) if self.select_queue else [])


def make_kb(kb_id: str, owner_id: str = USER_ID) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.UUID(kb_id), owner_id=owner_id, is_default=False)


def make_page(page_id: str, kb_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=page_id,
        kb_id=kb_id,
        page_type="entity",
        title="RAG架构",
        revision=3,
        status="active",
        source_doc_ids=["doc-1", "doc-2"],
        updated_at=None,
        content_path=f"wiki/{kb_id}/{page_id}.md",
    )


@pytest.fixture
def client(monkeypatch):
    """独立子应用：只挂 wiki 路由，覆盖 get_db / get_current_user。

    不用 src.main.app（避免 lifespan 触发真实 DB/Redis 连接）。
    """
    app = FastAPI()
    app.include_router(router, prefix="/api")

    db = FakeDB(select_queue=[])
    user = CurrentUser(user_id=USER_ID, is_authenticated=True)

    async def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: user

    # MinIO mock（正文预览）
    class FakeMinio:
        async def download_text_async(self, path):
            return "# RAG架构\n\n正文"

    monkeypatch.setattr("src.services.minio_service.MinioService.get_instance", _fake_get(FakeMinio()))
    return TestClient(app), db


def _fake_get(instance):
    async def fake_get():
        return instance

    return fake_get


KB_ID = str(uuid.uuid4())


class TestListPages:
    def test_returns_summaries(self, client):
        http, db = client
        db.select_queue = [[make_kb(KB_ID)], [make_page("page-1", KB_ID)]]
        resp = http.get(f"/api/knowledge_bases/{KB_ID}/wiki/pages")
        assert resp.status_code == 200
        body = resp.json()
        page = body["pages"][0]
        assert page["id"] == "page-1"
        assert page["page_type"] == "entity"
        assert page["revision"] == 3
        assert page["source_doc_count"] == 2

    def test_kb_not_found_404(self, client):
        http, db = client
        db.select_queue = [[None]]
        resp = http.get(f"/api/knowledge_bases/{KB_ID}/wiki/pages")
        assert resp.status_code == 404

    def test_invalid_kb_id_400(self, client):
        http, _ = client
        resp = http.get("/api/knowledge_bases/not-a-uuid/wiki/pages")
        assert resp.status_code == 400

    def test_forbidden_for_other_owner(self, client):
        http, db = client
        db.select_queue = [[make_kb(KB_ID, owner_id="someone-else")]]
        resp = http.get(f"/api/knowledge_bases/{KB_ID}/wiki/pages")
        assert resp.status_code == 403


class TestPageContent:
    def test_returns_content(self, client):
        http, db = client
        db.select_queue = [[make_kb(KB_ID)], [make_page("page-1", KB_ID)]]
        resp = http.get(f"/api/knowledge_bases/{KB_ID}/wiki/pages/page-1/content")
        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == "RAG架构"
        assert body["content"].startswith("# RAG架构")

    def test_page_not_found_404(self, client):
        http, db = client
        db.select_queue = [[make_kb(KB_ID)], [None]]
        resp = http.get(f"/api/knowledge_bases/{KB_ID}/wiki/pages/missing/content")
        assert resp.status_code == 404


class TestRebuild:
    def test_submits_background_task(self, client, monkeypatch):
        http, db = client
        db.select_queue = [[make_kb(KB_ID)]]

        calls = {}

        async def fake_rebuild(db_, kb_id, progress_cb=None):
            calls["kb_id"] = kb_id
            calls["progressed"] = progress_cb is not None
            return {"documents": 2, "pages_wiped": 3, "pages_created": 5,
                    "pages_updated": 1, "chunks_indexed": 9}

        async def noop(*a, **k):
            return None

        monkeypatch.setattr("src.services.wiki_rebuild.rebuild_kb_wiki", fake_rebuild)
        # 后台任务使用独立会话 —— mock 掉真实引擎
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_session():
            yield FakeDB([])

        monkeypatch.setattr("src.database.async_session_maker", lambda: fake_session())
        monkeypatch.setattr("src.services.progress_manager.create_upload_progress", lambda *a: None)
        monkeypatch.setattr("src.services.progress_manager.update_upload_progress", lambda *a, **k: None)
        monkeypatch.setattr("src.services.notification_service.notify_task_progress", noop)
        monkeypatch.setattr("src.services.notification_service.notify_task_completed", noop)
        monkeypatch.setattr("src.services.notification_service.notify_task_failed", noop)

        resp = http.post(f"/api/knowledge_bases/{KB_ID}/wiki/rebuild")
        assert resp.status_code == 200
        body = resp.json()
        assert body["upload_id"].startswith("wiki_rebuild_")
        # TestClient 响应后执行后台任务
        assert calls["kb_id"] == KB_ID
        assert calls["progressed"] is True
