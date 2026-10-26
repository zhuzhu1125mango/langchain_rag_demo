"""模型 Provider / Model / Route 注册表（P1）。

配置来源优先级：
1. ``MODEL_REGISTRY_FILE`` 指定的 JSON 文件
2. 默认 ``backend/data/model_registry.json``（存在时）
3. 以上都不存在 → 从现有 ``OLLAMA_*`` 环境变量自动生成 legacy registry

业务层只通过 ModelManager 获取模型；不要在各服务里直接依赖 ChatOllama。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.config import BASE_DIR, settings

logger = logging.getLogger("rag_system")

BACKEND_ROOT = os.path.dirname(BASE_DIR)
DEFAULT_REGISTRY_PATH = os.path.join(BACKEND_ROOT, "data", "model_registry.json")

SUPPORTED_PROVIDER_TYPES = {"ollama", "openai_compatible"}
SUPPORTED_CAPABILITIES = {"chat", "stream", "tools", "reasoning", "vision", "embedding", "rerank"}
TOOL_REQUIRED_TASKS = {"agent_decision"}
ALL_CHAT_TASKS = {
    "answer",
    "answer_direct",
    "fast",
    "intent_router",
    "title_generation",
    "query_rewrite",
    "verifier",
    "agent_decision",
    "wiki_compile",
    "doc_analyze",
}


class ModelRegistryError(RuntimeError):
    """模型注册表配置错误。"""


def _normalize_ollama_base_url(host: Optional[str]) -> Optional[str]:
    """把 OLLAMA_HOST（host / host:port / URL）规范成 ChatOllama 可用的 URL。"""
    if not host:
        return None
    host = host.strip().rstrip("/")
    if host.startswith(("http://", "https://")):
        return host
    if re.match(r"^[^/:]+:\d+$", host):
        return f"http://{host}"
    return f"http://{host}:11434"


class ProviderConfig(BaseModel):
    """模型服务连接。"""

    model_config = ConfigDict(extra="ignore")

    id: str
    type: str = "ollama"
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    api_key_env: Optional[str] = None
    api_key_required: bool = True
    headers: Dict[str, str] = Field(default_factory=dict)
    timeout_s: float = 60.0
    enabled: bool = True
    external: Optional[bool] = None

    def is_external(self) -> bool:
        if self.external is not None:
            return self.external
        return self.type != "ollama"

    def resolved_api_key(self) -> Optional[str]:
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.getenv(self.api_key_env)
        return None


class ModelConfig(BaseModel):
    """Provider 下的具体模型。"""

    model_config = ConfigDict(extra="ignore")

    id: str
    provider: str
    model: str
    display_name: str = ""
    capabilities: List[str] = Field(default_factory=lambda: ["chat", "stream"])
    context_window: int = 0
    max_output_tokens: int = 0
    embedding_dimension: int = 0
    temperature: Optional[float] = None

    def has(self, capability: str) -> bool:
        return capability in self.capabilities


class RouteConfig(BaseModel):
    """任务到模型的映射。"""

    model_config = ConfigDict(extra="ignore")

    primary: str
    fallbacks: List[str] = Field(default_factory=list)
    reasoning: str = "auto"  # native | prompt | disabled | auto
    temperature: Optional[float] = None
    max_output_tokens: Optional[int] = None
    timeout_s: Optional[float] = None
    allow_external_fallback: bool = False


class ModelRegistryConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: int = 1
    providers: List[ProviderConfig] = Field(default_factory=list)
    models: List[ModelConfig] = Field(default_factory=list)
    routes: Dict[str, RouteConfig] = Field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedModel:
    model: ModelConfig
    provider: ProviderConfig
    task: str
    fallback: bool = False


@dataclass
class ResolvedRoute:
    task: str
    primary: ResolvedModel
    fallbacks: List[ResolvedModel] = field(default_factory=list)
    config: Optional[RouteConfig] = None
    source: str = "legacy"


class ModelRegistry:
    """已校验的注册表快照。"""

    def __init__(self, config: ModelRegistryConfig, source: str):
        self.config = config
        self.source = source
        self.providers: Dict[str, ProviderConfig] = {p.id: p for p in config.providers}
        self.models: Dict[str, ModelConfig] = {m.id: m for m in config.models}
        self.routes: Dict[str, RouteConfig] = dict(config.routes)
        self._validate()

    @classmethod
    def load(cls, path: Optional[str] = None) -> "ModelRegistry":
        registry_path = Path(path or settings.model.MODEL_REGISTRY_FILE or DEFAULT_REGISTRY_PATH)
        if not registry_path.is_absolute():
            registry_path = Path(BACKEND_ROOT) / registry_path
        if not registry_path.exists():
            logger.info("模型注册文件不存在，使用 OLLAMA_* legacy 配置: %s", registry_path)
            return cls.bootstrap_legacy(source="legacy-env")

        try:
            raw = json.loads(registry_path.read_text(encoding="utf-8"))
            config = ModelRegistryConfig.model_validate(raw)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise ModelRegistryError(f"模型注册文件解析失败: {registry_path}: {exc}") from exc
        return cls(config, source=str(registry_path))

    @classmethod
    def bootstrap_legacy(cls, source: str = "legacy-env") -> "ModelRegistry":
        answer = settings.model.OLLAMA_MODEL_NAME or ""
        direct = settings.model.OLLAMA_DIRECT_MODEL_NAME or answer
        fast = settings.model.FAST_LLM_MODEL_NAME or direct
        embedding = settings.model.EMBEDDING_MODEL_NAME or ""
        reasoning_caps = ["reasoning"] if settings.model.OLLAMA_SUPPORTS_THINKING else []

        providers = [
            ProviderConfig(
                id="legacy-ollama",
                type="ollama",
                base_url=_normalize_ollama_base_url(settings.model.OLLAMA_HOST),
                api_key_required=False,
                external=False,
            )
        ]
        models = [
            ModelConfig(
                id="legacy-answer",
                provider="legacy-ollama",
                model=answer,
                display_name="本地推理模型",
                capabilities=["chat", "stream", "tools", *reasoning_caps],
                context_window=settings.model.OLLAMA_NUM_CTX,
            ),
            ModelConfig(
                id="legacy-direct",
                provider="legacy-ollama",
                model=direct,
                display_name="本地直答模型",
                capabilities=["chat", "stream", "tools"],
                context_window=settings.model.OLLAMA_NUM_CTX,
            ),
            ModelConfig(
                id="legacy-fast",
                provider="legacy-ollama",
                model=fast,
                display_name="本地轻量模型",
                capabilities=["chat", "stream", "tools"],
                context_window=settings.model.OLLAMA_NUM_CTX,
            ),
            ModelConfig(
                id="legacy-embedding",
                provider="legacy-ollama",
                model=embedding,
                display_name="本地向量模型",
                capabilities=["embedding"],
                embedding_dimension=settings.model.EMBEDDING_DIMENSION,
            ),
        ]

        title_model = settings.title_generation.TITLE_GENERATION_MODEL
        intent_model = settings.intent_router.INTENT_ROUTER_LLM_MODEL
        rewrite_model = settings.search.QUERY_REWRITE_MODEL
        wiki_model = settings.wiki_compile.WIKI_COMPILE_MODEL or None

        # 兼容旧的单任务模型覆盖：仍作为 Ollama 模型加入 legacy 路由
        extra_models: List[ModelConfig] = []
        route_overrides: Dict[str, str] = {}
        for task, name in (
            ("title_generation", title_model),
            ("intent_router", intent_model),
            ("query_rewrite", rewrite_model),
            ("wiki_compile", wiki_model),
        ):
            if name and name not in {m.model for m in models}:
                model_id = f"legacy-{task}"
                extra_models.append(
                    ModelConfig(
                        id=model_id,
                        provider="legacy-ollama",
                        model=name,
                        display_name="本地任务模型",
                        capabilities=["chat", "stream"],
                        context_window=settings.model.OLLAMA_NUM_CTX,
                    )
                )
                route_overrides[task] = model_id

        models.extend(extra_models)

        routes: Dict[str, RouteConfig] = {
            "answer": RouteConfig(primary="legacy-answer", fallbacks=["legacy-fast"]),
            "answer_direct": RouteConfig(primary="legacy-direct", fallbacks=["legacy-fast"]),
            "fast": RouteConfig(primary="legacy-fast", fallbacks=["legacy-direct", "legacy-answer"]),
            "intent_router": RouteConfig(primary=route_overrides.get("intent_router", "legacy-fast")),
            "title_generation": RouteConfig(primary=route_overrides.get("title_generation", "legacy-fast")),
            "query_rewrite": RouteConfig(primary=route_overrides.get("query_rewrite", "legacy-fast")),
            "verifier": RouteConfig(primary="legacy-fast"),
            "agent_decision": RouteConfig(primary="legacy-fast", fallbacks=["legacy-answer"]),
            "wiki_compile": RouteConfig(primary=route_overrides.get("wiki_compile", "legacy-fast")),
            "doc_analyze": RouteConfig(primary="legacy-fast"),
            "embedding": RouteConfig(primary="legacy-embedding"),
        }
        return cls(ModelRegistryConfig(providers=providers, models=models, routes=routes), source=source)

    def _validate(self) -> None:
        if not self.providers:
            raise ModelRegistryError("模型注册表缺少 providers")
        if not self.models:
            raise ModelRegistryError("模型注册表缺少 models")

        provider_ids = [p.id for p in self.config.providers]
        model_ids = [m.id for m in self.config.models]
        if len(provider_ids) != len(set(provider_ids)):
            raise ModelRegistryError("providers.id 不能重复")
        if len(model_ids) != len(set(model_ids)):
            raise ModelRegistryError("models.id 不能重复")

        for provider in self.config.providers:
            if provider.type not in SUPPORTED_PROVIDER_TYPES:
                raise ModelRegistryError(f"不支持的 provider.type: {provider.type}")
            if provider.type != "ollama" and not provider.base_url:
                raise ModelRegistryError(f"provider {provider.id} 缺少 base_url")
            if provider.api_key:
                logger.debug(
                    "provider %s 使用运行时注入的 api_key（DB 解密 / 本地开发）",
                    provider.id,
                )

        missing_routes = {"answer", "fast", "embedding"} - set(self.routes)
        if missing_routes:
            raise ModelRegistryError(f"模型注册表缺少必需路由: {sorted(missing_routes)}")

        for model in self.config.models:
            provider = self.providers.get(model.provider)
            if provider is None:
                raise ModelRegistryError(f"model {model.id} 引用了不存在的 provider: {model.provider}")
            unknown_caps = set(model.capabilities) - SUPPORTED_CAPABILITIES
            if unknown_caps:
                raise ModelRegistryError(f"model {model.id} 含未知能力: {sorted(unknown_caps)}")
            if model.has("embedding"):
                dimension = model.embedding_dimension or settings.model.EMBEDDING_DIMENSION
                if dimension != settings.model.EMBEDDING_DIMENSION:
                    raise ModelRegistryError(
                        f"embedding 模型 {model.id} 维度 {dimension} 与当前配置 "
                        f"{settings.model.EMBEDDING_DIMENSION} 不一致；切换 Embedding 必须重建索引"
                    )

        embedding_route = self.routes["embedding"]
        embedding_model = self.models.get(embedding_route.primary)
        if embedding_model is None or not embedding_model.has("embedding"):
            raise ModelRegistryError(f"embedding route 主模型必须声明 embedding: {embedding_route.primary}")

        for task, route in self.config.routes.items():
            if route.primary not in self.models:
                raise ModelRegistryError(f"route {task} 的 primary 不存在: {route.primary}")
            for fallback in route.fallbacks:
                if fallback not in self.models:
                    raise ModelRegistryError(f"route {task} 的 fallback 不存在: {fallback}")
            primary = self.models[route.primary]
            if task in TOOL_REQUIRED_TASKS and not primary.has("tools"):
                raise ModelRegistryError(f"route {task} 的主模型不支持 tools: {route.primary}")
            if route.reasoning == "native" and not primary.has("reasoning"):
                logger.warning("route %s 声明 native reasoning，但主模型未声明 reasoning", task)

    def get_model(self, model_id: str) -> ResolvedModel:
        model = self.models.get(model_id)
        if model is None:
            raise ModelRegistryError(f"模型不存在: {model_id}")
        provider = self.providers.get(model.provider)
        if provider is None:
            raise ModelRegistryError(f"模型 {model_id} 的 provider 不存在: {model.provider}")
        return ResolvedModel(model=model, provider=provider, task="")

    def resolve(self, task: str, *, require_tools: Optional[bool] = None) -> ResolvedRoute:
        route = self.routes.get(task)
        if route is None and task in ALL_CHAT_TASKS:
            route = self.routes.get("fast")
        if route is None:
            raise ModelRegistryError(f"未配置模型路由: {task}")

        primary = self.get_model(route.primary)
        if not primary.provider.enabled:
            raise ModelRegistryError(f"route {task} 的主 provider 已停用: {primary.provider.id}")

        needs_tools = task in TOOL_REQUIRED_TASKS if require_tools is None else require_tools
        fallbacks: List[ResolvedModel] = []
        for model_id in route.fallbacks:
            candidate = self.get_model(model_id)
            if not candidate.provider.enabled:
                continue
            if needs_tools and not candidate.model.has("tools"):
                logger.warning("route %s 跳过不支持 tools 的 fallback: %s", task, model_id)
                continue
            if (
                not route.allow_external_fallback
                and not primary.provider.is_external()
                and candidate.provider.is_external()
            ):
                logger.warning("route %s 跳过外部 fallback（未允许数据出域）: %s", task, model_id)
                continue
            fallbacks.append(candidate)

        return ResolvedRoute(
            task=task,
            primary=ResolvedModel(primary.model, primary.provider, task),
            fallbacks=[ResolvedModel(f.model, f.provider, task, fallback=True) for f in fallbacks],
            config=route,
            source=self.source,
        )

    def get_model_name(self, task: str) -> Optional[str]:
        route = self.routes.get(task)
        if route is None:
            return None
        model = self.models.get(route.primary)
        return model.model if model else None

    def get_embedding_model(self) -> ResolvedModel:
        route = self.routes.get("embedding")
        if route is None:
            raise ModelRegistryError("未配置 embedding route")
        model = self.get_model(route.primary)
        if not model.model.has("embedding"):
            raise ModelRegistryError(f"embedding route 主模型未声明 embedding: {route.primary}")
        return model

    def enabled_model_ids(self) -> List[str]:
        result = []
        for model in self.config.models:
            provider = self.providers.get(model.provider)
            if provider is not None and provider.enabled:
                result.append(model.id)
        return result
