"""Embedding 索引画像与迁移编排（MODEL-P3）。

- 记录 Milvus collection 当前由哪个 Embedding 模型/维度构建（`model_index_profile`）。
- 配置变更时作为 apply 安全闸门：画像不一致必须先重建索引。
- 重建走「影子集合 → 全量重向量化 → 原子换名」，失败保留线上集合；成功后保留备份
  集合支持回滚。
- 重建完成后全量失效语义缓存，并刷新进程内 Embedding 实例。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.models.model_registry import ModelIndexProfile, ModelRoute
from src.services.model_adapters import build_embeddings
from src.services.model_config_service import load_registry_config_from_db
from src.services.model_manager import model_manager
from src.services.model_registry import ModelRegistry, ModelRegistryConfig, ResolvedModel
from src.utils.error_handling import ErrorSeverity, log_exception

logger = logging.getLogger("rag_system")


class EmbeddingMigrationError(RuntimeError):
    """Embedding 迁移失败。"""


class EmbeddingMigrationConflict(EmbeddingMigrationError):
    """当前状态不允许该操作（API 返回 409）。"""


@dataclass(frozen=True)
class EmbeddingProfile:
    profile_id: str
    provider_id: str
    model_id: str
    model_name: str
    display_name: str
    dimension: int

    def as_dict(self) -> Dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "model_name": self.model_name,
            "display_name": self.display_name,
            "dimension": self.dimension,
        }


def profile_from_resolved(resolved: ResolvedModel) -> EmbeddingProfile:
    dimension = int(resolved.model.embedding_dimension or settings.model.EMBEDDING_DIMENSION)
    model_name = resolved.model.model
    return EmbeddingProfile(
        profile_id=f"{resolved.provider.id}:{resolved.model.id}:{model_name}:{dimension}",
        provider_id=resolved.provider.id,
        model_id=resolved.model.id,
        model_name=model_name,
        display_name=resolved.model.display_name or model_name,
        dimension=dimension,
    )


def profile_from_config(config: ModelRegistryConfig) -> EmbeddingProfile:
    registry = ModelRegistry(config, source="pending")
    return profile_from_resolved(registry.get_embedding_model())


def active_profile() -> EmbeddingProfile:
    return profile_from_resolved(model_manager.registry.get_embedding_model())


async def pending_config(db: AsyncSession) -> ModelRegistryConfig:
    """优先使用 DB 中待应用的配置；DB 为空时使用当前生效配置。"""
    config = await load_registry_config_from_db(db)
    return config or model_manager.registry.config


def _new_row(collection: str, profile: EmbeddingProfile, status: str = "ready") -> ModelIndexProfile:
    return ModelIndexProfile(
        collection_name=collection,
        profile_id=profile.profile_id,
        provider_id=profile.provider_id,
        model_id=profile.model_id,
        model_name=profile.model_name,
        dimension=profile.dimension,
        status=status,
        chunk_count=0,
        last_error="",
    )


async def _milvus():
    from src.services.milvus_service import MilvusService

    return await MilvusService.get_instance()


async def _get_or_adopt(
    db: AsyncSession,
    profile: EmbeddingProfile,
    *,
    probe: bool = True,
) -> Optional[ModelIndexProfile]:
    """读取画像行；首次运行时根据线上集合维度补建。

    P3 上线前没有画像数据：若集合维度与目标一致，直接采纳目标画像；否则记为
    ``unknown:{dim}``，等待管理员重建。Milvus 不可达时不写库，返回 None。
    """
    collection = settings.milvus.MILVUS_COLLECTION_NAME
    row = await db.get(ModelIndexProfile, collection)
    if row is not None:
        return row
    if not probe:
        return None
    try:
        milvus = await _milvus()
        dimension = await milvus.describe_embedding_dimension(collection)
        chunk_count = await milvus._collection_count(collection) if dimension else 0
    except Exception as exc:
        # Milvus 不可达：画像状态未知，本次不写库，由调用方按 unknown 处理
        log_exception(
            logger, exc,
            action="embedding_migration.probe_profile",
            severity=ErrorSeverity.RECOVERABLE,
            message="读取 Milvus 索引画像失败，状态按未知处理",
            collection=collection,
        )
        return None

    if dimension is None:
        row = _new_row(collection, profile)
    elif dimension == profile.dimension:
        row = _new_row(collection, profile)
        row.chunk_count = chunk_count
    else:
        row = ModelIndexProfile(
            collection_name=collection,
            profile_id=f"unknown:{dimension}",
            provider_id="",
            model_id="",
            model_name="(未知模型)",
            dimension=dimension,
            status="ready",
            chunk_count=chunk_count,
            last_error="",
        )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


def _status_dict(row: Optional[ModelIndexProfile], target: EmbeddingProfile) -> Dict[str, Any]:
    if row is None:
        return {
            "collection": settings.milvus.MILVUS_COLLECTION_NAME,
            "indexed": None,
            "target": target.as_dict(),
            "in_sync": False,
            "needs_rebuild": False,
            "can_rollback": False,
            "status": "unknown",
            "chunk_count": 0,
            "backup_collection": None,
            "last_error": "",
        }
    in_sync = row.profile_id == target.profile_id and row.dimension == target.dimension
    return {
        "collection": row.collection_name,
        "indexed": {
            "profile_id": row.profile_id,
            "provider_id": row.provider_id,
            "model_id": row.model_id,
            "model_name": row.model_name,
            "dimension": row.dimension,
        },
        "target": target.as_dict(),
        "in_sync": in_sync,
        "needs_rebuild": not in_sync or row.status == "failed",
        "can_rollback": bool(row.backup_collection and row.previous_model_id),
        "status": row.status,
        "chunk_count": row.chunk_count,
        "backup_collection": row.backup_collection,
        "last_error": row.last_error or "",
    }


async def get_index_status(db: AsyncSession, *, probe: bool = True) -> Dict[str, Any]:
    target = profile_from_config(await pending_config(db))
    row = await _get_or_adopt(db, target, probe=probe)
    return _status_dict(row, target)


async def assert_apply_allowed(db: AsyncSession, config: Optional[ModelRegistryConfig] = None) -> None:
    """apply 安全闸门：Embedding 画像与索引不一致时禁止应用（要求先重建）。"""
    target = profile_from_config(config or await pending_config(db))
    row = await db.get(ModelIndexProfile, settings.milvus.MILVUS_COLLECTION_NAME)
    if row is None:
        return
    if row.status == "rebuilding":
        raise EmbeddingMigrationConflict("Embedding 索引正在重建，请完成后再应用配置")
    if row.profile_id != target.profile_id or row.dimension != target.dimension:
        raise EmbeddingMigrationConflict(
            "Embedding 模型/维度已变更，必须先在「模型服务 → 索引」中重建向量索引后再应用配置"
        )


async def config_matches_indexed_profile(db: AsyncSession, config: ModelRegistryConfig) -> bool:
    """启动加载 DB 配置前的轻量校验：不一致则不应用，避免重启后检索维度错配。"""
    row = await db.get(ModelIndexProfile, settings.milvus.MILVUS_COLLECTION_NAME)
    if row is None:
        return True
    if row.status == "rebuilding":
        return False
    target = profile_from_config(config)
    return row.profile_id == target.profile_id and row.dimension == target.dimension


_migration_lock = asyncio.Lock()
_rebuild_tasks: set = set()


async def _invalidate_caches() -> None:
    try:
        from src.services.semantic_cache_service import SemanticCacheService

        await SemanticCacheService().invalidate_all()
    except Exception as exc:  # 缓存不可用不能回滚已完成的数据迁移
        log_exception(
            logger, exc,
            action="embedding_migration.invalidate_caches",
            severity=ErrorSeverity.RECOVERABLE,
            message="Embedding 迁移后失效语义缓存失败，旧向量缓存将按 TTL 自然过期",
        )


async def rebuild_index(
    db: AsyncSession,
    *,
    force: bool = False,
    progress_cb: Optional[Callable[[int, int], Any]] = None,
) -> Dict[str, Any]:
    """按 DB 待应用配置重建 Milvus 索引；成功后自动应用该配置。"""
    if _migration_lock.locked():
        raise EmbeddingMigrationConflict("已有 Embedding 索引重建任务在运行")

    async with _migration_lock:
        config = await pending_config(db)
        target = profile_from_config(config)
        row = await _get_or_adopt(db, target)
        if row is None:
            raise EmbeddingMigrationError("无法读取 Milvus 索引状态，请确认向量库可用后重试")

        in_sync = row.profile_id == target.profile_id and row.dimension == target.dimension
        if in_sync and not force and row.status != "failed":
            raise EmbeddingMigrationConflict("当前索引已与目标 Embedding 一致，无需重建")

        previous = {
            "profile_id": row.profile_id,
            "provider_id": row.provider_id,
            "model_id": row.model_id,
            "model_name": row.model_name,
            "dimension": row.dimension,
        }
        row.status = "rebuilding"
        row.last_error = ""
        await db.commit()

        milvus = await _milvus()
        target_embeddings = build_embeddings(ModelRegistry(config, source="pending").get_embedding_model())
        shadow: Optional[str] = None
        backup: Optional[str] = None
        applied = False
        try:
            result = await milvus.rebuild_collection(target.dimension, target_embeddings, progress_cb)
            shadow = result["shadow_collection"]
            backup = await milvus.swap_collection(shadow)

            # 索引已切换后应用 DB 配置（若来自 DB），保证在线路由与索引一致
            db_config = await load_registry_config_from_db(db)
            if db_config is not None:
                model_manager.apply_registry_config(db_config, source="database")
                applied = True
            milvus.refresh_embeddings()

            row.profile_id = target.profile_id
            row.provider_id = target.provider_id
            row.model_id = target.model_id
            row.model_name = target.model_name
            row.dimension = target.dimension
            row.chunk_count = int(result.get("chunk_count") or 0)
            row.status = "ready"
            row.previous_profile_id = previous["profile_id"] or None
            row.previous_model_id = previous["model_id"] or None
            row.previous_dimension = int(previous["dimension"] or 0) or None
            row.backup_collection = backup
            await db.commit()
            await _invalidate_caches()
            logger.info(
                "Embedding 索引重建完成: %s -> %s, chunks=%d, backup=%s",
                previous["profile_id"],
                target.profile_id,
                row.chunk_count,
                backup,
            )
            return _status_dict(row, target)
        except Exception as exc:
            log_exception(
                logger, exc,
                action="embedding_migration.rebuild",
                message="Embedding 索引重建失败，开始回滚",
                previous_profile=previous["profile_id"],
                target_profile=target.profile_id,
            )
            # 已经完成集合交换时先还原旧集合；尚未交换则清理影子集合
            if backup:
                try:
                    await milvus.restore_collection(backup)
                except Exception as restore_exc:
                    log_exception(
                        logger, restore_exc,
                        action="embedding_migration.restore_backup",
                        message="重建失败后还原备份集合失败，需人工介入",
                        backup_collection=backup,
                    )
                if applied:
                    try:
                        model_manager.apply_registry_config(
                            ModelRegistryConfig.model_validate(previous_config_snapshot(previous, config)), source="rollback"
                        )
                    except Exception as route_exc:
                        log_exception(
                            logger, route_exc,
                            action="embedding_migration.rollback_route",
                            message="重建失败后还原在线配置失败，需人工介入",
                        )
                milvus.refresh_embeddings()
            elif shadow:
                try:
                    await milvus.drop_collection_by_name(shadow)
                except Exception as cleanup_exc:
                    log_exception(
                        logger, cleanup_exc,
                        action="embedding_migration.drop_shadow",
                        severity=ErrorSeverity.RECOVERABLE,
                        message="清理影子集合失败，需人工删除残留集合",
                        shadow_collection=shadow,
                    )

            row.status = "failed"
            row.last_error = f"{type(exc).__name__}: {exc}"[:500]
            try:
                await db.commit()
            except Exception as commit_exc:
                # 画像是"重建状态"的唯一来源，写不进去会让闸门失效，必须上报
                log_exception(
                    logger, commit_exc,
                    action="embedding_migration.mark_failed",
                    message="记录 Embedding 迁移失败状态时提交失败，索引画像可能停留在 rebuilding",
                )
            raise EmbeddingMigrationError(str(exc)) from exc


def previous_config_snapshot(previous: dict, current_config: ModelRegistryConfig) -> dict:
    """构造回滚所需的原始配置：把 embedding route 指回旧模型（若仍存在）。"""
    data = current_config.model_dump()
    previous_model_id = previous.get("model_id")
    if not previous_model_id:
        return data
    if not any(model.get("id") == previous_model_id for model in data.get("models", [])):
        return data
    data.setdefault("routes", {})["embedding"]["primary"] = previous_model_id
    return data


async def rollback_index(db: AsyncSession) -> Dict[str, Any]:
    """把备份集合换回并恢复旧 Embedding 路由。"""
    if _migration_lock.locked():
        raise EmbeddingMigrationConflict("已有 Embedding 索引任务在运行")

    async with _migration_lock:
        row = await db.get(ModelIndexProfile, settings.milvus.MILVUS_COLLECTION_NAME)
        if row is None or not row.backup_collection or not row.previous_model_id:
            raise EmbeddingMigrationConflict("当前没有可回滚的索引备份")
        if row.status == "rebuilding":
            raise EmbeddingMigrationConflict("索引正在重建，无法回滚")

        # 先准备旧配置，确认旧模型仍存在，避免换完集合无法切回
        db_config = await load_registry_config_from_db(db)
        if db_config is not None:
            if not any(model.id == row.previous_model_id for model in db_config.models):
                raise EmbeddingMigrationConflict("旧 Embedding 模型已从配置中删除，无法回滚")
            route = await db.scalar(select(ModelRoute).where(ModelRoute.task == "embedding"))
            if route is not None and route.primary_model_id != row.previous_model_id:
                route.primary_model_id = row.previous_model_id
                await db.commit()
            config = await load_registry_config_from_db(db)
        else:
            config = model_manager.registry.config.model_copy(deep=True)
            if not any(model.id == row.previous_model_id for model in config.models):
                raise EmbeddingMigrationConflict("旧 Embedding 模型已从配置中删除，无法回滚")
        if config.routes["embedding"].primary != row.previous_model_id:
            config.routes["embedding"].primary = row.previous_model_id

        registry = ModelRegistry(config, source="rollback")
        old_model = registry.get_model(row.previous_model_id)
        if not old_model.model.has("embedding"):
            raise EmbeddingMigrationConflict("回滚目标模型不支持 embedding")

        milvus = await _milvus()
        archive = await milvus.restore_collection(row.backup_collection)
        try:
            model_manager.apply_registry_config(config, source="rollback")
            milvus.refresh_embeddings()
        except Exception as exc:
            log_exception(
                logger, exc,
                action="embedding_migration.rollback_apply_config",
                message="索引已回滚但恢复在线配置失败，需人工介入",
                backup_collection=row.backup_collection,
            )
            raise EmbeddingMigrationError(f"索引已回滚，但恢复模型配置失败: {exc}") from exc

        target_profile = profile_from_resolved(old_model)
        row.profile_id = target_profile.profile_id
        row.provider_id = target_profile.provider_id
        row.model_id = target_profile.model_id
        row.model_name = target_profile.model_name
        row.dimension = target_profile.dimension
        row.chunk_count = await milvus._collection_count(row.collection_name)
        row.status = "rolled_back"
        row.previous_profile_id = None
        row.previous_model_id = None
        row.previous_dimension = None
        row.backup_collection = None
        row.last_error = f"已回滚，重建版本归档为 {archive}"[:500]
        await db.commit()
        await _invalidate_caches()
        return _status_dict(row, target_profile)


def schedule_rebuild(force: bool = False) -> None:
    """后台执行重建（API 立即返回，前端轮询状态）。"""
    if _migration_lock.locked():
        raise EmbeddingMigrationConflict("已有 Embedding 索引重建任务在运行")

    async def _run() -> None:
        from src.database import async_session_maker

        async with async_session_maker() as db:
            try:
                await rebuild_index(db, force=force)
            except Exception as exc:
                # 后台任务无调用方兜底，这里是唯一记录点，必须留堆栈
                log_exception(
                    logger, exc,
                    action="embedding_migration.background_rebuild",
                    message="后台 Embedding 重建任务失败",
                    force=force,
                )

    task = asyncio.create_task(_run())
    _rebuild_tasks.add(task)
    task.add_done_callback(_rebuild_tasks.discard)
