"""MODEL-P3：Embedding 索引画像、重建/回滚状态机测试。"""

from types import SimpleNamespace

import pytest

import src.services.embedding_migration as em
from src.config import settings
from src.models.model_registry import ModelIndexProfile
from src.services.model_registry import ModelConfig, ModelRegistryConfig, ProviderConfig, RouteConfig

LIVE = settings.milvus.MILVUS_COLLECTION_NAME


def _config(embedding_id: str = "emb-new", dimension: int = 512, model_name: str = "bge-new") -> ModelRegistryConfig:
    models = [
        ModelConfig(id="answer", provider="p1", model="qwen"),
        ModelConfig(id="fast", provider="p1", model="qwen-fast"),
        ModelConfig(
            id=embedding_id,
            provider="p1",
            model=model_name,
            capabilities=["embedding"],
            embedding_dimension=dimension,
        ),
    ]
    if embedding_id != "emb-old":
        models.append(
            ModelConfig(
                id="emb-old",
                provider="p1",
                model="bge-old",
                capabilities=["embedding"],
                embedding_dimension=1024,
            )
        )
    return ModelRegistryConfig(
        providers=[ProviderConfig(id="p1", type="ollama", base_url="http://ollama", external=False)],
        models=models,
        routes={
            "answer": RouteConfig(primary="answer"),
            "fast": RouteConfig(primary="fast"),
            "embedding": RouteConfig(primary=embedding_id),
        },
    )


class _DB:
    def __init__(self, row=None, route=None):
        self.row = row
        self.route = route
        self.commits = 0

    async def get(self, model, key):
        return self.row

    async def commit(self):
        self.commits += 1

    async def scalar(self, stmt):
        return self.route


def _row(**overrides) -> ModelIndexProfile:
    values = {
        "collection_name": LIVE,
        "profile_id": "p1:emb-old:bge-old:1024",
        "provider_id": "p1",
        "model_id": "emb-old",
        "model_name": "bge-old",
        "dimension": 1024,
        "status": "ready",
        "chunk_count": 10,
        "last_error": "",
    }
    values.update(overrides)
    return ModelIndexProfile(**values)


class _FakeMilvus:
    def __init__(self, fail_rebuild=False, fail_swap=False):
        self.fail_rebuild = fail_rebuild
        self.fail_swap = fail_swap
        self.dropped = []
        self.refreshed = 0
        self.restored = []

    async def rebuild_collection(self, dimension, embeddings, progress_cb=None):
        if self.fail_rebuild:
            raise RuntimeError("embedding down")
        return {"shadow_collection": "rag_chunks__rebuild_x", "chunk_count": 12, "source_count": 10, "skipped": 0}

    async def swap_collection(self, shadow):
        if self.fail_swap:
            raise RuntimeError("rename failed")
        return "rag_chunks__backup_x"

    async def restore_collection(self, backup):
        self.restored.append(backup)
        return "rag_chunks__rolledback_x"

    async def drop_collection_by_name(self, name):
        self.dropped.append(name)

    async def _collection_count(self, name):
        return 10

    def refresh_embeddings(self):
        self.refreshed += 1


@pytest.mark.asyncio
async def test_apply_gate_blocks_embedding_profile_mismatch():
    target_config = _config()
    db = _DB(_row())

    with pytest.raises(em.EmbeddingMigrationConflict, match="重建"):
        await em.assert_apply_allowed(db, target_config)


@pytest.mark.asyncio
async def test_apply_gate_allows_matching_profile_and_rejects_rebuilding():
    target_config = _config(embedding_id="emb-old", dimension=1024, model_name="bge-old")
    target = em.profile_from_config(target_config)

    await em.assert_apply_allowed(_DB(_row(profile_id=target.profile_id, dimension=target.dimension)), target_config)

    with pytest.raises(em.EmbeddingMigrationConflict, match="重建"):
        await em.assert_apply_allowed(_DB(_row(status="rebuilding")), target_config)


@pytest.mark.asyncio
async def test_rebuild_index_swaps_collection_and_records_profile(monkeypatch):
    config = _config()
    target = em.profile_from_config(config)
    row = _row()
    db = _DB(row)
    milvus = _FakeMilvus()
    applied = []

    async def _pending(_db):
        return config

    async def _get_or_adopt(_db, _target, **_kwargs):
        return row

    async def _milvus():
        return milvus

    async def _load_config(_db):
        return config

    async def _invalidate():
        return None

    monkeypatch.setattr(em, "pending_config", _pending)
    monkeypatch.setattr(em, "_get_or_adopt", _get_or_adopt)
    monkeypatch.setattr(em, "_milvus", _milvus)
    monkeypatch.setattr(em, "build_embeddings", lambda resolved: object())
    monkeypatch.setattr(em, "load_registry_config_from_db", _load_config)
    monkeypatch.setattr(em, "_invalidate_caches", _invalidate)
    monkeypatch.setattr(em.model_manager, "apply_registry_config", lambda cfg, source="database": applied.append(source))

    result = await em.rebuild_index(db, force=True)

    assert result["in_sync"] is True
    assert row.status == "ready"
    assert row.profile_id == target.profile_id
    assert row.dimension == target.dimension
    assert row.backup_collection == "rag_chunks__backup_x"
    assert row.previous_model_id == "emb-old"
    assert row.chunk_count == 12
    assert applied == ["database"]
    assert milvus.refreshed == 1
    assert db.commits >= 2


@pytest.mark.asyncio
async def test_rebuild_failure_cleans_shadow_and_marks_failed(monkeypatch):
    config = _config()
    row = _row()
    db = _DB(row)
    milvus = _FakeMilvus(fail_swap=True)

    monkeypatch.setattr(em, "pending_config", lambda _db: _async(config))
    monkeypatch.setattr(em, "_get_or_adopt", lambda _db, _target, **kw: _async(row))
    monkeypatch.setattr(em, "_milvus", lambda: _async(milvus))
    monkeypatch.setattr(em, "build_embeddings", lambda resolved: object())
    monkeypatch.setattr(em, "load_registry_config_from_db", lambda _db: _async(config))
    monkeypatch.setattr(em, "_invalidate_caches", lambda: _async(None))

    with pytest.raises(em.EmbeddingMigrationError):
        await em.rebuild_index(db, force=True)

    assert row.status == "failed"
    assert "rename failed" in row.last_error
    assert milvus.dropped == ["rag_chunks__rebuild_x"]


@pytest.mark.asyncio
async def test_rollback_restores_backup_and_previous_route(monkeypatch):
    config = _config()  # embedding route -> emb-new (512)
    row = _row(
        status="ready",
        profile_id="p1:emb-new:bge-new:512",
        provider_id="p1",
        model_id="emb-new",
        model_name="bge-new",
        dimension=512,
        backup_collection="rag_chunks__backup_x",
        previous_profile_id="p1:emb-old:bge-old:1024",
        previous_model_id="emb-old",
        previous_dimension=1024,
    )
    route = SimpleNamespace(task="embedding", primary_model_id="emb-new")
    db = _DB(row, route)
    milvus = _FakeMilvus()

    async def _load_config(_db):
        return config

    async def _milvus():
        return milvus

    monkeypatch.setattr(em, "load_registry_config_from_db", _load_config)
    monkeypatch.setattr(em, "_milvus", _milvus)
    monkeypatch.setattr(em, "_invalidate_caches", lambda: _async(None))
    monkeypatch.setattr(em.model_manager, "apply_registry_config", lambda cfg, source="rollback": None)

    result = await em.rollback_index(db)

    assert route.primary_model_id == "emb-old"
    assert row.status == "rolled_back"
    assert row.model_id == "emb-old"
    assert row.dimension == 1024
    assert row.backup_collection is None
    assert milvus.restored == ["rag_chunks__backup_x"]
    assert result["in_sync"] is True


def _async(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner()
