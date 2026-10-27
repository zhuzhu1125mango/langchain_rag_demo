"""模型 Provider / Model / Route 管理 API（P2，仅管理员）。

所有接口通过 ``require_admin`` 鉴权；Provider API Key 只写不读，
GET 响应仅返回 ``has_api_key`` / ``api_key_hint``。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import CurrentUser, require_admin
from src.config import settings
from src.database import get_db
from src.models.model_registry import ModelDeployment, ModelProvider
from src.services.audit_service import record_audit
from src.services.model_config_service import (
    ModelConfigConflict,
    ModelConfigError,
    ModelConfigNotFound,
    apply_registry_from_db,
    create_model,
    create_provider,
    delete_model,
    delete_provider,
    registry_snapshot,
    safe_model,
    safe_provider,
    test_provider,
    update_model,
    update_provider,
    upsert_routes,
)
from src.services.model_health import provider_health
from src.services.model_registry import ProviderConfig
from src.services.model_usage import model_usage

logger = logging.getLogger("rag_system")

router = APIRouter(
    prefix="/admin/models",
    tags=["admin-models"],
    dependencies=[Depends(require_admin)],
)


class ProviderUpsert(BaseModel):
    name: Optional[str] = None
    provider_type: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    api_key_env: Optional[str] = None
    api_key_required: Optional[bool] = None
    headers: Optional[Dict[str, str]] = None
    timeout_s: Optional[float] = None
    external: Optional[bool] = None
    enabled: Optional[bool] = None


class ModelUpsert(BaseModel):
    provider_id: Optional[str] = None
    model_name: Optional[str] = None
    display_name: Optional[str] = None
    capabilities: Optional[List[str]] = None
    context_window: Optional[int] = None
    max_output_tokens: Optional[int] = None
    embedding_dimension: Optional[int] = None
    temperature: Optional[float] = None
    input_cost_per_1k: Optional[float] = None
    output_cost_per_1k: Optional[float] = None
    price_currency: Optional[str] = None
    enabled: Optional[bool] = None


class RouteUpsert(BaseModel):
    task: str
    primary_model_id: str
    fallback_model_ids: List[str] = Field(default_factory=list)
    reasoning: str = "auto"
    temperature: Optional[float] = None
    max_output_tokens: Optional[int] = None
    timeout_s: Optional[float] = None
    allow_external_fallback: bool = False


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ModelConfigNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ModelConfigConflict):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


async def _audit(action: str, resource_id: str, detail: Optional[dict], user: CurrentUser) -> None:
    await record_audit(
        action=action,
        resource_type="model_config",
        resource_id=resource_id,
        detail=detail or {},
        actor_user_id=user.user_id,
    )


@router.get("/snapshot")
async def get_snapshot(db: AsyncSession = Depends(get_db)):
    try:
        return await registry_snapshot(db)
    except Exception as exc:
        raise _http_error(exc)


@router.get("/usage")
async def get_model_usage(days: int = Query(7, ge=1, le=90), db: AsyncSession = Depends(get_db)):
    """模型用量（持久化聚合 + 进程内未落库增量）与 Provider 健康（MODEL-P2.1）。"""
    from src.services.model_usage_store import merge_usage_rows, query_usage_totals

    persisted: List[dict] = []
    try:
        persisted = await query_usage_totals(db, days=days)
    except Exception as exc:
        logger.warning("读取模型用量聚合失败，仅返回进程内增量: %s", exc)
    return {
        "usage": merge_usage_rows(persisted, model_usage.pending_snapshot()),
        "health": provider_health.snapshot(),
        "days": days,
    }


@router.get("/index/status")
async def get_embedding_index_status(db: AsyncSession = Depends(get_db)):
    """Embedding 索引画像：当前索引 vs 待应用配置、是否需重建、可否回滚（MODEL-P3）。"""
    from src.services.embedding_migration import get_index_status

    try:
        return await get_index_status(db)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"读取索引状态失败: {exc}")


@router.post("/index/rebuild")
async def rebuild_embedding_index(
    force: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_admin),
):
    """按 DB 待应用 Embedding 配置重建索引（后台任务，前端轮询状态）。"""
    from src.services.embedding_migration import EmbeddingMigrationConflict, schedule_rebuild

    try:
        schedule_rebuild(force=force)
    except EmbeddingMigrationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    await _audit(
        "model.index.rebuild",
        settings.milvus.MILVUS_COLLECTION_NAME,
        {"force": force},
        current_user,
    )
    return {"status": "accepted", "force": force}


@router.post("/index/rollback")
async def rollback_embedding_index(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_admin),
):
    """把上一次重建前的备份集合换回，并恢复旧 Embedding 路由。"""
    from src.services.embedding_migration import (
        EmbeddingMigrationConflict,
        EmbeddingMigrationError,
        rollback_index,
    )

    try:
        result = await rollback_index(db)
    except EmbeddingMigrationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except EmbeddingMigrationError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    await _audit(
        "model.index.rollback",
        settings.milvus.MILVUS_COLLECTION_NAME,
        {"profile_id": result.get("indexed", {}).get("profile_id", "")},
        current_user,
    )
    return result


@router.get("/providers")
async def list_providers(db: AsyncSession = Depends(get_db)):
    try:
        rows = (await db.execute(select(ModelProvider).order_by(ModelProvider.name))).scalars().all()
        return [await safe_provider(row) for row in rows]
    except Exception as exc:
        raise _http_error(exc)


@router.post("/providers", status_code=status.HTTP_201_CREATED)
async def add_provider(
    payload: ProviderUpsert,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_admin),
):
    try:
        row = await create_provider(db, payload.model_dump(exclude_unset=True))
    except Exception as exc:
        raise _http_error(exc)
    await _audit("model.provider.create", row.id, {"name": row.name, "type": row.provider_type}, current_user)
    return await safe_provider(row)


@router.patch("/providers/{provider_id}")
async def edit_provider(
    provider_id: str,
    payload: ProviderUpsert,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_admin),
):
    try:
        row = await update_provider(db, provider_id, payload.model_dump(exclude_unset=True))
    except Exception as exc:
        raise _http_error(exc)
    await _audit("model.provider.update", row.id, {"name": row.name}, current_user)
    return await safe_provider(row)


@router.delete("/providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_provider(
    provider_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_admin),
):
    try:
        await delete_provider(db, provider_id)
    except Exception as exc:
        raise _http_error(exc)
    await _audit("model.provider.delete", provider_id, {}, current_user)


@router.post("/providers/test")
async def test_provider_draft(payload: ProviderUpsert):
    data = payload.model_dump(exclude_unset=True)
    try:
        provider = ProviderConfig(
            id="draft",
            type=data.get("provider_type") or "ollama",
            base_url=data.get("base_url"),
            api_key=data.get("api_key"),
            api_key_env=data.get("api_key_env"),
            api_key_required=bool(data.get("api_key_required", True)),
            headers=data.get("headers") or {},
            timeout_s=float(data.get("timeout_s") or 60),
            external=bool(data.get("external", True)),
        )
        return await test_provider(provider)
    except Exception as exc:
        raise _http_error(exc)


@router.post("/providers/{provider_id}/test")
async def test_saved_provider(provider_id: str, db: AsyncSession = Depends(get_db)):
    from src.services.model_config_service import _provider_config

    row = await db.get(ModelProvider, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Provider 不存在")
    try:
        return await test_provider(_provider_config(row))
    except Exception as exc:
        raise _http_error(exc)


@router.get("/providers/{provider_id}/models")
async def discover_models(provider_id: str, db: AsyncSession = Depends(get_db)):
    from src.services.model_config_service import _provider_config

    row = await db.get(ModelProvider, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Provider 不存在")
    try:
        return await test_provider(_provider_config(row))
    except Exception as exc:
        raise _http_error(exc)


@router.get("/deployments")
async def list_models(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(ModelDeployment).order_by(ModelDeployment.model_name))).scalars().all()
    return [safe_model(row) for row in rows]


@router.post("/deployments", status_code=status.HTTP_201_CREATED)
async def add_model(
    payload: ModelUpsert,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_admin),
):
    try:
        row = await create_model(db, payload.model_dump(exclude_unset=True))
    except Exception as exc:
        raise _http_error(exc)
    await _audit("model.deployment.create", row.id, {"model": row.model_name}, current_user)
    return safe_model(row)


@router.patch("/deployments/{model_id}")
async def edit_model(
    model_id: str,
    payload: ModelUpsert,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_admin),
):
    try:
        row = await update_model(db, model_id, payload.model_dump(exclude_unset=True))
    except Exception as exc:
        raise _http_error(exc)
    await _audit("model.deployment.update", row.id, {"model": row.model_name}, current_user)
    return safe_model(row)


@router.delete("/deployments/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_model(
    model_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_admin),
):
    try:
        await delete_model(db, model_id)
    except Exception as exc:
        raise _http_error(exc)
    await _audit("model.deployment.delete", model_id, {}, current_user)


@router.get("/routes")
async def list_routes(db: AsyncSession = Depends(get_db)):
    return (await registry_snapshot(db))["routes"]


@router.put("/routes")
async def save_routes(
    routes: List[RouteUpsert],
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_admin),
):
    try:
        await upsert_routes(db, [item.model_dump() for item in routes])
    except Exception as exc:
        raise _http_error(exc)
    await _audit("model.routes.update", "routes", {"tasks": [item.task for item in routes]}, current_user)
    return await registry_snapshot(db)


@router.post("/apply")
async def apply_registry(
    db: AsyncSession = Depends(get_db),
    current_user: CurrentUser = Depends(require_admin),
):
    from src.services.embedding_migration import EmbeddingMigrationConflict, assert_apply_allowed

    try:
        await assert_apply_allowed(db)
    except EmbeddingMigrationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    try:
        registry = await apply_registry_from_db(db)
    except Exception as exc:
        raise _http_error(exc)
    await _audit("model.registry.apply", registry.source, {}, current_user)
    return {
        "source": registry.source,
        "providers": len(registry.providers),
        "models": len(registry.models),
        "routes": len(registry.routes),
    }
