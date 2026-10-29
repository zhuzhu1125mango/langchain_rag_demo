"""模型配置管理服务（P2）。

负责 DB 行与 ModelRegistryConfig 的转换、Provider/Model/Route CRUD、
连接测试 / 模型发现，以及把 DB 配置应用为在线 registry。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.model_registry import ModelDeployment, ModelProvider, ModelRoute
from src.utils.error_handling import log_exception
from src.services.model_registry import (
    ModelConfig,
    ModelRegistry,
    ModelRegistryConfig,
    ProviderConfig,
    RouteConfig,
    _normalize_ollama_base_url,
)
from src.services.model_secret_cipher import (
    ModelSecretCipherError,
    decrypt_secret,
    encrypt_secret,
    secret_hint,
)

logger = logging.getLogger("rag_system")


class ModelConfigError(RuntimeError):
    """模型配置管理错误（API 返回 400）。"""


class ModelConfigNotFound(ModelConfigError):
    """配置对象不存在（API 返回 404）。"""


class ModelConfigConflict(ModelConfigError):
    """配置对象被引用/重名（API 返回 409）。"""


def _provider_config(row: ModelProvider, *, include_secret: bool = True) -> ProviderConfig:
    api_key = None
    if include_secret and row.api_key_ciphertext:
        try:
            api_key = decrypt_secret(row.api_key_ciphertext)
        except ModelSecretCipherError as exc:
            raise ModelConfigError(f"Provider {row.name} 的 API Key 无法解密，请重新录入") from exc
    return ProviderConfig(
        id=row.id,
        type=row.provider_type,
        base_url=row.base_url,
        api_key=api_key,
        api_key_env=row.api_key_env,
        api_key_required=bool(row.api_key_required),
        headers=row.headers or {},
        timeout_s=float(row.timeout_s or 60),
        enabled=bool(row.enabled),
        external=bool(row.external),
    )


def _model_config(row: ModelDeployment) -> ModelConfig:
    return ModelConfig(
        id=row.id,
        provider=row.provider_id,
        model=row.model_name,
        display_name=row.display_name or row.model_name,
        capabilities=list(row.capabilities or []),
        context_window=int(row.context_window or 0),
        max_output_tokens=int(row.max_output_tokens or 0),
        embedding_dimension=int(row.embedding_dimension or 0),
        temperature=row.temperature,
        input_cost_per_1k=row.input_cost_per_1k,
        output_cost_per_1k=row.output_cost_per_1k,
        price_currency=row.price_currency or "USD",
    )


async def load_registry_config_from_db(db: AsyncSession) -> Optional[ModelRegistryConfig]:
    """DB 为空返回 None；否则转换成可校验的 ModelRegistryConfig。"""
    providers = (await db.execute(select(ModelProvider))).scalars().all()
    if not providers:
        return None
    deployments = (await db.execute(select(ModelDeployment))).scalars().all()
    routes = (await db.execute(select(ModelRoute))).scalars().all()

    provider_configs = [_provider_config(p) for p in providers]
    model_configs = [_model_config(m) for m in deployments]
    route_configs: Dict[str, RouteConfig] = {}
    for route in routes:
        route_configs[route.task] = RouteConfig(
            primary=route.primary_model_id,
            fallbacks=list(route.fallback_model_ids or []),
            reasoning=route.reasoning or "auto",
            temperature=route.temperature,
            max_output_tokens=route.max_output_tokens,
            timeout_s=route.timeout_s,
            allow_external_fallback=bool(route.allow_external_fallback),
        )
    return ModelRegistryConfig(
        version=1,
        providers=provider_configs,
        models=model_configs,
        routes=route_configs,
    )


async def apply_registry_from_db(db: AsyncSession) -> ModelRegistry:
    """校验 DB 配置并替换在线 registry；校验失败抛 ModelConfigError。"""
    config = await load_registry_config_from_db(db)
    if config is None:
        raise ModelConfigError("数据库尚未配置任何模型 Provider")
    try:
        registry = ModelRegistry(config, source="database")
    except Exception as exc:
        # 包成 ModelConfigError 是为了给管理员可读提示，但原始异常类型就此丢失
        # （调用方只能看到「配置不合法」）；先按原始异常分级留痕再上抛
        log_exception(
            logger,
            exc,
            action="model.registry.build",
            message="DB 模型配置构建在线 registry 失败",
        )
        raise ModelConfigError(str(exc)) from exc
    from src.services.model_manager import model_manager

    model_manager.apply_registry_config(config, source="database")
    return registry


async def safe_provider(row: ModelProvider) -> Dict[str, Any]:
    key = None
    if row.api_key_ciphertext:
        try:
            key = decrypt_secret(row.api_key_ciphertext)
        except ModelSecretCipherError:
            key = None
    return {
        "id": row.id,
        "name": row.name,
        "provider_type": row.provider_type,
        "base_url": row.base_url,
        "api_key_env": row.api_key_env,
        "api_key_required": bool(row.api_key_required),
        "has_api_key": bool(row.api_key_ciphertext),
        "api_key_hint": secret_hint(key) if key else "",
        "headers": row.headers or {},
        "timeout_s": row.timeout_s,
        "external": bool(row.external),
        "enabled": bool(row.enabled),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def safe_model(row: ModelDeployment) -> Dict[str, Any]:
    return {
        "id": row.id,
        "provider_id": row.provider_id,
        "model_name": row.model_name,
        "display_name": row.display_name or row.model_name,
        "capabilities": list(row.capabilities or []),
        "context_window": row.context_window,
        "max_output_tokens": row.max_output_tokens,
        "embedding_dimension": row.embedding_dimension,
        "temperature": row.temperature,
        "input_cost_per_1k": row.input_cost_per_1k,
        "output_cost_per_1k": row.output_cost_per_1k,
        "price_currency": row.price_currency or "USD",
        "enabled": bool(row.enabled),
    }


async def registry_snapshot(db: AsyncSession) -> Dict[str, Any]:
    providers = (await db.execute(select(ModelProvider).order_by(ModelProvider.name))).scalars().all()
    models = (await db.execute(select(ModelDeployment).order_by(ModelDeployment.model_name))).scalars().all()
    routes = (await db.execute(select(ModelRoute).order_by(ModelRoute.task))).scalars().all()
    model_names = {m.id: m.model_name for m in models}
    return {
        "providers": [await safe_provider(p) for p in providers],
        "models": [safe_model(m) for m in models],
        "routes": [
            {
                "task": r.task,
                "primary_model_id": r.primary_model_id,
                "primary_model_name": model_names.get(r.primary_model_id, ""),
                "fallback_model_ids": list(r.fallback_model_ids or []),
                "fallback_model_names": [model_names.get(x, x) for x in (r.fallback_model_ids or [])],
                "reasoning": r.reasoning,
                "temperature": r.temperature,
                "max_output_tokens": r.max_output_tokens,
                "timeout_s": r.timeout_s,
                "allow_external_fallback": bool(r.allow_external_fallback),
            }
            for r in routes
        ],
    }


async def create_provider(db: AsyncSession, payload: Dict[str, Any]) -> ModelProvider:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ModelConfigError("Provider 名称不能为空")
    exists = await db.scalar(select(ModelProvider.id).where(ModelProvider.name == name))
    if exists:
        raise ModelConfigConflict(f"Provider 名称已存在: {name}")
    provider = ModelProvider(
        name=name,
        provider_type=payload.get("provider_type") or "ollama",
        base_url=payload.get("base_url") or None,
        api_key_env=payload.get("api_key_env") or None,
        api_key_required=bool(payload.get("api_key_required", True)),
        headers=payload.get("headers") or {},
        timeout_s=float(payload.get("timeout_s") or 60),
        external=bool(payload.get("external", True)),
        enabled=bool(payload.get("enabled", True)),
    )
    api_key = payload.get("api_key")
    if api_key:
        provider.api_key_ciphertext = encrypt_secret(str(api_key))
    db.add(provider)
    await db.commit()
    await db.refresh(provider)
    return provider


async def update_provider(db: AsyncSession, provider_id: str, payload: Dict[str, Any]) -> ModelProvider:
    provider = await db.get(ModelProvider, provider_id)
    if provider is None:
        raise ModelConfigNotFound("Provider 不存在")
    if "name" in payload and payload["name"]:
        name = str(payload["name"]).strip()
        conflict = await db.scalar(
            select(ModelProvider.id).where(ModelProvider.name == name, ModelProvider.id != provider_id)
        )
        if conflict:
            raise ModelConfigConflict(f"Provider 名称已存在: {name}")
        provider.name = name
    for field in ("provider_type", "base_url", "api_key_env"):
        if field in payload:
            setattr(provider, field, payload[field] or None)
    for field in ("api_key_required", "external", "enabled"):
        if field in payload:
            setattr(provider, field, bool(payload[field]))
    if "timeout_s" in payload and payload["timeout_s"] is not None:
        provider.timeout_s = float(payload["timeout_s"])
    if "headers" in payload:
        provider.headers = payload["headers"] or {}
    if "api_key" in payload:
        api_key = payload.get("api_key")
        if api_key:
            provider.api_key_ciphertext = encrypt_secret(str(api_key))
        elif api_key == "":
            provider.api_key_ciphertext = None
    await db.commit()
    await db.refresh(provider)
    return provider


async def delete_provider(db: AsyncSession, provider_id: str) -> None:
    provider = await db.get(ModelProvider, provider_id)
    if provider is None:
        raise ModelConfigNotFound("Provider 不存在")
    deployment = await db.scalar(
        select(ModelDeployment.id).where(ModelDeployment.provider_id == provider_id).limit(1)
    )
    if deployment:
        raise ModelConfigConflict("Provider 下仍有模型，请先删除模型")
    await db.delete(provider)
    await db.commit()


def _price_value(field: str, value: Any) -> Optional[float]:
    """校验成本单价：空值表示未配置，负数/非数字返回 400。"""
    if value is None or value == "":
        return None
    try:
        price = float(value)
    except (TypeError, ValueError) as exc:
        raise ModelConfigError(f"{field} 必须是数字") from exc
    if price < 0:
        raise ModelConfigError(f"{field} 不能为负数")
    return price


def _currency_value(value: Any) -> str:
    return (str(value or "USD").strip().upper() or "USD")[:8]


async def create_model(db: AsyncSession, payload: Dict[str, Any]) -> ModelDeployment:
    provider_id = payload.get("provider_id")
    if not provider_id or await db.get(ModelProvider, provider_id) is None:
        raise ModelConfigNotFound("Provider 不存在")
    model = ModelDeployment(
        provider_id=provider_id,
        model_name=str(payload.get("model_name") or "").strip(),
        display_name=str(payload.get("display_name") or "").strip(),
        capabilities=list(payload.get("capabilities") or []),
        context_window=int(payload.get("context_window") or 0),
        max_output_tokens=int(payload.get("max_output_tokens") or 0),
        embedding_dimension=int(payload.get("embedding_dimension") or 0),
        temperature=payload.get("temperature"),
        input_cost_per_1k=_price_value("input_cost_per_1k", payload.get("input_cost_per_1k")),
        output_cost_per_1k=_price_value("output_cost_per_1k", payload.get("output_cost_per_1k")),
        price_currency=_currency_value(payload.get("price_currency")),
        enabled=bool(payload.get("enabled", True)),
    )
    if not model.model_name:
        raise ModelConfigError("模型名不能为空")
    db.add(model)
    await db.commit()
    await db.refresh(model)
    return model


async def update_model(db: AsyncSession, model_id: str, payload: Dict[str, Any]) -> ModelDeployment:
    model = await db.get(ModelDeployment, model_id)
    if model is None:
        raise ModelConfigNotFound("模型不存在")
    for field in ("provider_id", "model_name", "display_name"):
        if field in payload:
            setattr(model, field, payload[field])
    if "capabilities" in payload:
        model.capabilities = list(payload["capabilities"] or [])
    for field in ("context_window", "max_output_tokens", "embedding_dimension"):
        if field in payload:
            setattr(model, field, int(payload[field] or 0))
    if "temperature" in payload:
        model.temperature = payload["temperature"]
    for field in ("input_cost_per_1k", "output_cost_per_1k"):
        if field in payload:
            setattr(model, field, _price_value(field, payload[field]))
    if "price_currency" in payload:
        model.price_currency = _currency_value(payload["price_currency"])
    if "enabled" in payload:
        model.enabled = bool(payload["enabled"])
    await db.commit()
    await db.refresh(model)
    return model


async def delete_model(db: AsyncSession, model_id: str) -> None:
    model = await db.get(ModelDeployment, model_id)
    if model is None:
        raise ModelConfigNotFound("模型不存在")
    routes = (await db.execute(select(ModelRoute))).scalars().all()
    for route in routes:
        if route.primary_model_id == model_id or model_id in (route.fallback_model_ids or []):
            raise ModelConfigConflict(f"模型仍被任务路由 {route.task} 引用")
    await db.delete(model)
    await db.commit()


async def upsert_routes(db: AsyncSession, routes: List[Dict[str, Any]]) -> None:
    existing = {r.task: r for r in (await db.execute(select(ModelRoute))).scalars().all()}
    seen = set()
    for payload in routes:
        task = str(payload.get("task") or "").strip()
        if not task:
            raise ModelConfigError("route.task 不能为空")
        if task in seen:
            raise ModelConfigError(f"route.task 重复: {task}")
        seen.add(task)
        primary = payload.get("primary_model_id")
        if not primary or await db.get(ModelDeployment, primary) is None:
            raise ModelConfigNotFound(f"route {task} 的主模型不存在")
        fallbacks = list(payload.get("fallback_model_ids") or [])
        for fallback in fallbacks:
            if await db.get(ModelDeployment, fallback) is None:
                raise ModelConfigNotFound(f"route {task} 的 fallback 不存在: {fallback}")
        row = existing.get(task)
        if row is None:
            row = ModelRoute(task=task)
            db.add(row)
        row.primary_model_id = primary
        row.fallback_model_ids = fallbacks
        row.reasoning = payload.get("reasoning") or "auto"
        row.temperature = payload.get("temperature")
        row.max_output_tokens = payload.get("max_output_tokens")
        row.timeout_s = payload.get("timeout_s")
        row.allow_external_fallback = bool(payload.get("allow_external_fallback", False))
    for task, row in existing.items():
        if task not in seen:
            await db.delete(row)
    await db.commit()


def _validate_url(url: Optional[str]) -> str:
    if not url or not url.startswith(("http://", "https://")):
        raise ModelConfigError("base_url 必须是 http(s) 地址")
    return url.rstrip("/")


async def test_provider(provider: ProviderConfig) -> Dict[str, Any]:
    """管理员连接测试 / 模型发现；API Key 不进入返回值。"""
    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=False) as client:
            if provider.type == "ollama":
                base = _normalize_ollama_base_url(provider.base_url or None)
                if not base:
                    raise ModelConfigError("Ollama base_url 未配置")
                resp = await client.get(f"{base}/api/tags")
                resp.raise_for_status()
                models = [item.get("name", "") for item in resp.json().get("models", [])]
                return {"ok": True, "models": models, "status_code": resp.status_code}
            base = _validate_url(provider.base_url)
            headers = dict(provider.headers or {})
            key = provider.resolved_api_key()
            if key:
                headers["Authorization"] = f"Bearer {key}"
            resp = await client.get(f"{base}/models", headers=headers)
            resp.raise_for_status()
            data = resp.json()
            models = [item.get("id", "") for item in data.get("data", [])]
            return {"ok": True, "models": models, "status_code": resp.status_code}
    except ModelConfigError:
        raise
    except Exception as exc:
        # 预期控制流：管理员点「连接测试」，网络/鉴权失败就是要告诉他哪里不对，
        # 返回值里带 error 文案，不需要额外留痕（否则每次误配都刷一条告警）
        return {"ok": False, "models": [], "error": str(exc)[:300]}
