"""MODEL-P3：Milvus 影子集合重建/交换/回滚单元测试。"""

from unittest.mock import AsyncMock

import pytest
from pymilvus import DataType

from src.config import settings
from src.services.milvus_service import MilvusService

LIVE = settings.milvus.MILVUS_COLLECTION_NAME


def _service() -> MilvusService:
    svc = MilvusService()
    svc.client = AsyncMock()
    svc._sparse_enabled = False
    svc._has_heading_path = True
    svc._has_source_kind = True
    svc._build_schema = AsyncMock(return_value="schema")
    svc._create_indexes = AsyncMock()
    return svc


_FIELDS = [
    {"name": "id", "type": DataType.VARCHAR},
    {"name": "kb_id", "type": DataType.VARCHAR},
    {"name": "document_id", "type": DataType.VARCHAR},
    {"name": "content", "type": DataType.VARCHAR},
    {"name": "source", "type": DataType.VARCHAR},
    {"name": "chunk_index", "type": DataType.INT64},
    {"name": "heading_path", "type": DataType.VARCHAR},
    {"name": "source_kind", "type": DataType.VARCHAR},
]


@pytest.mark.asyncio
async def test_rebuild_collection_reembeds_scalar_rows_into_shadow():
    svc = _service()
    svc.client.has_collection.return_value = True
    svc.client.describe_collection.return_value = {"fields": _FIELDS}
    svc.client.query.side_effect = [
        [{"count(*)": 2}],
        [
            {"id": "c1", "kb_id": "kb", "document_id": "d1", "content": "hello", "source": "a.md", "chunk_index": 0},
            {"id": "c2", "kb_id": "kb", "document_id": "d1", "content": "world", "source": "a.md", "chunk_index": 1},
        ],
    ]
    embeddings = AsyncMock()
    embeddings.aembed_documents.return_value = [[0.1] * 512, [0.2] * 512]

    result = await svc.rebuild_collection(512, embeddings)

    assert result["chunk_count"] == 2
    assert result["shadow_collection"].startswith(f"{LIVE}__rebuild_")
    svc._build_schema.assert_awaited_once_with(dimension=512)
    inserted = svc.client.insert.await_args.kwargs
    assert inserted["collection_name"] == result["shadow_collection"]
    assert len(inserted["data"]) == 2
    assert inserted["data"][0]["embedding"] == [0.1] * 512
    svc.client.flush.assert_awaited_once()
    svc._create_indexes.assert_awaited_once_with(collection_name=result["shadow_collection"])
    svc.client.load_collection.assert_awaited_once_with(result["shadow_collection"])


@pytest.mark.asyncio
async def test_rebuild_rejects_dimension_mismatch_from_embedding_model():
    svc = _service()
    svc.client.has_collection.return_value = True
    svc.client.describe_collection.return_value = {"fields": _FIELDS}
    svc.client.query.side_effect = [
        [{"count(*)": 1}],
        [{"id": "c1", "content": "hello", "document_id": "d1"}],
    ]
    embeddings = AsyncMock()
    embeddings.aembed_documents.return_value = [[0.1] * 1024]

    with pytest.raises(RuntimeError, match="维度"):
        await svc.rebuild_collection(512, embeddings)


@pytest.mark.asyncio
async def test_swap_and_restore_collection_rename_flow():
    svc = _service()
    svc.client.rename_collection = AsyncMock()
    svc.client.load_collection = AsyncMock()
    svc.client.has_collection.return_value = True

    backup = await svc.swap_collection("live__rebuild_x")

    assert backup.startswith(f"{LIVE}__backup_")
    assert svc.client.rename_collection.await_args_list[0].args == (LIVE, backup)
    assert svc.client.rename_collection.await_args_list[1].args == ("live__rebuild_x", LIVE)

    archive = await svc.restore_collection(backup)

    assert archive.startswith(f"{LIVE}__rolledback_")
    assert svc.client.rename_collection.await_args_list[-2].args == (LIVE, archive)
    assert svc.client.rename_collection.await_args_list[-1].args == (backup, LIVE)
