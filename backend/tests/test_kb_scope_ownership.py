"""知识库推荐 / 图谱的 owner 范围限定回归测试（P0-3）。

背景：
- `POST /knowledge_bases/recommend` 原先不接收 kb_ids，检索层按 `None` 处理，
  等价于在**全部用户**的知识库中检索——任何登录用户都能拿到他人知识库的
  推荐结果（kb_id、相关性分数）。
- `POST /knowledge_bases/knowledge_graph` 把客户端传入的 kb_ids 原样下传，
  无归属校验，且不传时同样退化为全量检索。

关键陷阱（这组用例存在的主要原因）：
检索层对 `kb_ids` 的判断是 `if kb_ids and len(kb_ids) > 0`，**空列表是假值**，
会被当作"不限定范围"，最终 `filter_expr=None` 即 Milvus 全量检索。
因此"用户没有知识库时传空列表"这种看似自然的写法反而会全量泄露，
必须在 API 层短路返回。

本组用例直接调用路由协程，不经过 TestClient / app lifespan，
因此无需 PostgreSQL / Milvus / Redis，可随单元测试全量运行。
"""

import uuid

import pytest
from fastapi import HTTPException

import src.api.knowledge_base as kb_api
from src.auth import CurrentUser

USER_A_KB = "11111111-1111-1111-1111-111111111111"
FOREIGN_KB = "22222222-2222-2222-2222-222222222222"


class _FakeScalars:
    """伪造 `db.execute(...).scalars()` 的返回。"""

    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self._rows


class _FakeDB:
    """只服务于本组用例的假会话：固定返回「当前用户拥有的知识库」行。

    `_owned_kb_ids` 与 `validate_kb_ownership` 都走 `db.execute`，
    返回同一组行即可分别覆盖这两条路径。
    """

    def __init__(self, owned_ids):
        self._owned_ids = [uuid.UUID(i) for i in owned_ids]

    async def execute(self, _stmt):
        return _FakeScalars(self._owned_ids)


class _ExplodingRAGChain:
    """一旦被调用即失败：用于断言"未发起任何检索"。"""

    def __init__(self):
        self.called = False

    async def recommend_knowledge_bases(self, question, top_k=3, kb_ids=None):
        self.called = True
        raise AssertionError("不应在无可访问知识库时发起推荐检索")

    async def generate_knowledge_graph(self, kb_ids=None):
        self.called = True
        raise AssertionError("不应在无可访问知识库时发起图谱检索")


class _CapturingRAGChain:
    """记录下传的 kb_ids，用于断言 owner 过滤确实生效。"""

    def __init__(self):
        self.received = None

    async def recommend_knowledge_bases(self, question, top_k=3, kb_ids=None):
        self.received = kb_ids
        return []

    async def generate_knowledge_graph(self, kb_ids=None):
        self.received = kb_ids
        return {"nodes": [], "edges": [], "summary": "ok"}


def _patch_chain(monkeypatch, chain):
    async def _get_instance(*_args, **_kwargs):
        return chain

    async def _get_vector_store(*_args, **_kwargs):
        return object()

    monkeypatch.setattr(kb_api.RAGChain, "get_instance", _get_instance)
    monkeypatch.setattr(kb_api.VectorStoreManager, "get_instance", _get_vector_store)


@pytest.fixture
def user_a():
    return CurrentUser(user_id="user_a", is_authenticated=True)


class TestRecommendScope:
    """`/knowledge_bases/recommend` 的候选范围必须由服务端按 owner 解析。"""

    @pytest.mark.asyncio
    async def test_no_owned_kb_short_circuits_without_retrieval(self, monkeypatch, user_a):
        """用户没有任何知识库 → 直接返回空，且不得发起检索。

        若无此短路，候选范围会退化为"全部用户的知识库"。
        """
        chain = _ExplodingRAGChain()
        _patch_chain(monkeypatch, chain)

        result = await kb_api.recommend_knowledge_bases(
            kb_api.KBRecommendationRequest(question="问题"),
            current_user=user_a,
            db=_FakeDB([]),
        )

        assert result == []
        assert chain.called is False

    @pytest.mark.asyncio
    async def test_owned_kb_ids_forwarded_as_scope(self, monkeypatch, user_a):
        """用户拥有的 KB 必须作为检索范围下传，形成 owner 过滤。"""
        chain = _CapturingRAGChain()
        _patch_chain(monkeypatch, chain)

        await kb_api.recommend_knowledge_bases(
            kb_api.KBRecommendationRequest(question="问题"),
            current_user=user_a,
            db=_FakeDB([USER_A_KB]),
        )

        assert chain.received == [USER_A_KB]


class TestKnowledgeGraphScope:
    """`/knowledge_bases/knowledge_graph` 的范围限定与归属校验。"""

    @pytest.mark.asyncio
    async def test_defaults_to_owned_kb_when_unspecified(self, monkeypatch, user_a):
        """未传 kb_ids → 默认取当前用户的知识库，而非全量。"""
        chain = _CapturingRAGChain()
        _patch_chain(monkeypatch, chain)

        await kb_api.generate_knowledge_graph(
            kb_api.KnowledgeGraphRequest(),
            current_user=user_a,
            db=_FakeDB([USER_A_KB]),
        )

        assert chain.received == [USER_A_KB]

    @pytest.mark.asyncio
    async def test_foreign_kb_id_rejected(self, monkeypatch, user_a):
        """显式传入他人的 kb_id → 403，且不得发起检索。"""
        chain = _ExplodingRAGChain()
        _patch_chain(monkeypatch, chain)

        with pytest.raises(HTTPException) as exc:
            await kb_api.generate_knowledge_graph(
                kb_api.KnowledgeGraphRequest(kb_ids=[FOREIGN_KB]),
                current_user=user_a,
                db=_FakeDB([USER_A_KB]),
            )

        assert exc.value.status_code == 403
        assert chain.called is False

    @pytest.mark.asyncio
    async def test_no_owned_kb_short_circuits_without_retrieval(self, monkeypatch, user_a):
        """用户没有任何知识库且未指定 → 返回空图谱，不发起全量检索。"""
        chain = _ExplodingRAGChain()
        _patch_chain(monkeypatch, chain)

        result = await kb_api.generate_knowledge_graph(
            kb_api.KnowledgeGraphRequest(),
            current_user=user_a,
            db=_FakeDB([]),
        )

        assert result["nodes"] == [] and result["edges"] == []
        assert chain.called is False

    @pytest.mark.asyncio
    async def test_malformed_kb_id_returns_400(self, monkeypatch, user_a):
        """非法 UUID → 400（而非 500）。"""
        chain = _ExplodingRAGChain()
        _patch_chain(monkeypatch, chain)

        with pytest.raises(HTTPException) as exc:
            await kb_api.generate_knowledge_graph(
                kb_api.KnowledgeGraphRequest(kb_ids=["not-a-uuid"]),
                current_user=user_a,
                db=_FakeDB([USER_A_KB]),
            )

        assert exc.value.status_code == 400
        assert chain.called is False
