"""模型注册与任务路由管理器。

P1 起，模型选择由 Provider/Model/Route registry 决定：
- 有 ``MODEL_REGISTRY_FILE`` / ``backend/data/model_registry.json`` 时按文件路由
- 否则从现有 ``OLLAMA_*`` 自动生成 legacy registry（行为不变）
- 业务层只调用本模块，不直接 new ChatOllama / OpenAIEmbeddings

旧的 ``check_availability`` / ``get_model_for_task`` 保留为兼容入口。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config import settings
from src.services.model_adapters import build_embeddings, build_route_models
from src.utils.error_handling import ErrorSeverity, log_exception
from src.services.model_registry import (
    DEFAULT_REGISTRY_PATH,
    THINKING_ALWAYS_ON,
    THINKING_OFF,
    THINKING_SWITCHABLE,
    ModelRegistry,
    ResolvedModel,
    ResolvedRoute,
    RouteConfig,
    resolve_thinking_control,
)

logger = logging.getLogger(__name__)


@dataclass
class ModelInfo:
    """模型信息。"""

    name: str
    task: str
    available: bool
    error: Optional[str] = None


class ModelManager:
    """模型注册表 + 任务路由 + 适配器工厂。"""

    DEFAULT_TASK_ROLES: Dict[str, str] = {
        "answer": "OLLAMA_MODEL_NAME",
        "answer_direct": "OLLAMA_DIRECT_MODEL_NAME",
        "fast": "FAST_LLM_MODEL_NAME",
        "embedding": "EMBEDDING_MODEL_NAME",
        "intent_router": "FAST_LLM_MODEL_NAME",
        "title_generation": "FAST_LLM_MODEL_NAME",
        "query_rewrite": "FAST_LLM_MODEL_NAME",
    }

    DEFAULT_FALLBACK_CHAINS: Dict[str, List[str]] = {
        "answer": ["OLLAMA_MODEL_NAME", "FAST_LLM_MODEL_NAME"],
        "answer_direct": ["OLLAMA_DIRECT_MODEL_NAME", "FAST_LLM_MODEL_NAME"],
        "fast": ["FAST_LLM_MODEL_NAME", "OLLAMA_DIRECT_MODEL_NAME", "OLLAMA_MODEL_NAME"],
        "embedding": ["EMBEDDING_MODEL_NAME"],
        "intent_router": ["FAST_LLM_MODEL_NAME", "OLLAMA_MODEL_NAME"],
        "title_generation": ["FAST_LLM_MODEL_NAME", "OLLAMA_MODEL_NAME"],
        "query_rewrite": ["FAST_LLM_MODEL_NAME", "OLLAMA_MODEL_NAME"],
    }

    def __init__(
        self,
        task_roles: Optional[Dict[str, str]] = None,
        fallback_chains: Optional[Dict[str, List[str]]] = None,
        ollama_host: Optional[str] = None,
    ):
        self.task_roles = task_roles or dict(self.DEFAULT_TASK_ROLES)
        self.fallback_chains = fallback_chains or self.DEFAULT_FALLBACK_CHAINS
        self.ollama_host = ollama_host or settings.model.OLLAMA_HOST
        self._availability_cache: Dict[str, bool] = {}
        self._cache_lock = asyncio.Lock()
        self._embeddings_cache: Dict[str, Any] = {}
        self._registry: Optional[ModelRegistry] = None
        self._registry_mtime: Optional[float] = None
        self._registry_path: Optional[Path] = None
        self._db_registry_active = False
        self._registry_revision = 0

    def _reset_provider_health(self) -> None:
        """registry 变更后清空 Provider 熔断状态（保留用量累计）。"""
        try:
            from src.services.model_health import provider_health

            provider_health.reset()
        except Exception:
            # 熔断状态只是进程内缓存，重置失败不会影响路由正确性
            logger.debug("重置 Provider 健康状态失败", exc_info=True)

    # ------------------------------------------------------------------
    # Registry lifecycle
    # ------------------------------------------------------------------
    def _resolved_registry_path(self) -> Path:
        raw = settings.model.MODEL_REGISTRY_FILE or DEFAULT_REGISTRY_PATH
        path = Path(raw)
        if not path.is_absolute():
            # 相对路径以 backend/ 为根（容器内 /app，挂载 backend/data）
            path = Path(__file__).resolve().parents[2] / path
        return path

    def _load_registry(self, path: Path) -> None:
        try:
            registry = ModelRegistry.load(str(path))
        except Exception as exc:
            # 热加载失败时保留旧快照，避免一个坏文件把在线服务打挂
            if self._registry is not None:
                log_exception(
                    logger, exc,
                    action="model_manager.registry.reload",
                    message="模型注册表重载失败，继续使用上一版配置",
                    registry_path=str(path),
                )
                return
            raise
        self._registry = registry
        self._db_registry_active = False
        self._registry_revision += 1
        self._registry_path = path
        self._registry_mtime = path.stat().st_mtime if path.exists() else None
        self._reset_provider_health()
        logger.info("模型注册表已加载: source=%s, providers=%d, models=%d, routes=%d",
                    registry.source, len(registry.providers), len(registry.models), len(registry.routes))

    def _get_registry(self) -> ModelRegistry:
        if self._db_registry_active and self._registry is not None:
            return self._registry
        path = self._resolved_registry_path()
        current_mtime = path.stat().st_mtime if path.exists() else None
        if (
            self._registry is None
            or self._registry_path != path
            or current_mtime != self._registry_mtime
        ):
            self._load_registry(path)
        assert self._registry is not None
        return self._registry

    def reload_registry(self) -> ModelRegistry:
        """强制重载文件注册表（P1 兼容；DB registry 请用 reload_from_db）。"""
        self._db_registry_active = False
        self._registry = None
        self._registry_mtime = None
        return self._get_registry()

    def apply_registry_config(self, config: Any, source: str = "database") -> ModelRegistry:
        """用已校验的配置替换在线 registry（P2 管理 API 写入后调用）。"""
        registry = ModelRegistry(config, source=source)
        self._registry = registry
        self._db_registry_active = True
        self._registry_path = None
        self._registry_mtime = None
        self._registry_revision += 1
        self._embeddings_cache.clear()
        self._reset_provider_health()
        logger.info(
            "模型 registry 已应用: source=%s, providers=%d, models=%d, routes=%d, revision=%d",
            source, len(registry.providers), len(registry.models), len(registry.routes), self._registry_revision,
        )
        return registry

    async def load_from_db(self) -> bool:
        """启动时加载 DB registry；DB 为空/未配置时返回 False（回退文件/env）。"""
        try:
            from src.database import async_session
            from src.services.model_config_service import load_registry_config_from_db

            async with async_session() as db:
                config = await load_registry_config_from_db(db)
                if config is None:
                    return False
                # MODEL-P3：Embedding 画像与索引不一致时跳过应用，避免重启后检索维度错配
                from src.services.embedding_migration import config_matches_indexed_profile

                if not await config_matches_indexed_profile(db, config):
                    logger.warning(
                        "DB 模型配置的 Embedding 画像与 Milvus 索引不一致，已跳过应用；"
                        "请在管理页「模型服务 → 索引」完成重建后再应用/重启"
                    )
                    return False
            self.apply_registry_config(config, source="database")
            return True
        except Exception as exc:
            # DB 未初始化/无配置是正常启动路径，回退文件与 env 即可
            log_exception(
                logger, exc,
                action="model_manager.registry.load_from_db",
                severity=ErrorSeverity.RECOVERABLE,
                message="DB 模型 registry 加载失败，回退文件/env 配置",
            )
            return False

    @property
    def registry(self) -> ModelRegistry:
        """当前注册表快照（DB 优先；否则文件 mtime 热加载）。"""
        return self._get_registry()

    @property
    def registry_generation(self) -> str:
        """配置代次：路由/模型热切换时用于缓存失效。"""
        registry = self._get_registry()
        return f"{registry.source}:{self._registry_revision}:{registry.config.version}"

    # ------------------------------------------------------------------
    # 兼容方法
    # ------------------------------------------------------------------
    def _resolve_model_name(self, model_ref: str) -> str:
        if hasattr(settings.model, model_ref):
            return getattr(settings.model, model_ref) or ""
        return model_ref

    async def check_availability(self, model_name: str) -> bool:
        """Ollama 模型可用性检查（旧接口，保留给运维/兼容代码）。"""
        if model_name in self._availability_cache:
            return self._availability_cache[model_name]
        try:
            import ollama

            async with self._cache_lock:
                if model_name in self._availability_cache:
                    return self._availability_cache[model_name]
                client = ollama.Client(host=self.ollama_host) if self.ollama_host else ollama.Client()
                response = await asyncio.to_thread(client.list)
                models = response.get("models", [])
                names = {m.get("name", "") for m in models}
                names.update(m.get("model", "") for m in models)
                available = model_name in names
                self._availability_cache[model_name] = available
                if not available:
                    logger.warning("模型 %s 未在本地 Ollama 中找到", model_name)
                return available
        except Exception as exc:
            # 探测失败不能误判为"模型不可用"（会阻断请求），按可用继续
            log_exception(
                logger, exc,
                action="model_manager.availability_check",
                severity=ErrorSeverity.RECOVERABLE,
                message="检查模型可用性失败，按可用处理",
                model=model_name,
            )
            return True

    def clear_availability_cache(self) -> None:
        self._availability_cache.clear()

    async def get_model_for_task(self, task: str, preferred: Optional[str] = None) -> str:
        """返回任务主模型名（兼容旧调用；不再决定 Provider/Endpoint）。"""
        if preferred:
            return preferred
        registry = self._get_registry()
        name = registry.get_model_name(task)
        if name is not None:
            return name
        chain = self.fallback_chains.get(task, [])
        for ref in chain:
            candidate = self._resolve_model_name(ref)
            if candidate:
                return candidate
        return settings.model.OLLAMA_MODEL_NAME or ""

    async def get_available_models(self) -> List[ModelInfo]:
        """按已配置路由列出模型及启用状态。"""
        registry = self._get_registry()
        result: List[ModelInfo] = []
        for task, route in registry.routes.items():
            model = registry.get_model(route.primary)
            result.append(
                ModelInfo(
                    name=model.model.model,
                    task=task,
                    available=model.provider.enabled,
                    error=None if model.provider.enabled else "provider disabled",
                )
            )
        return result

    # ------------------------------------------------------------------
    # 工厂入口
    # ------------------------------------------------------------------
    async def get_chat_llm(
        self,
        task: str,
        *,
        streaming: bool = False,
        think: Optional[bool] = False,
        num_ctx: Optional[int] = None,
        temperature: Optional[float] = None,
        timeout: Optional[float] = None,
        preferred: Optional[str] = None,
        max_output_tokens: Optional[int] = None,
    ) -> Any:
        """按路由构造聊天模型（含 fallback 组）。

        ``think`` 只对声明支持 reasoning 的模型生效；不支持时忽略，不伪造思考。
        ``preferred`` 仅为旧 API 兼容保留，注册表 route 才是唯一事实来源。
        """
        if preferred:
            logger.warning("get_chat_llm.preferred 已弃用，模型路由请配置 registry: task=%s", task)
        registry = self._get_registry()
        route: ResolvedRoute = registry.resolve(
            task,
            require_tools=(task == "agent_decision"),
        )
        return build_route_models(
            route,
            streaming=streaming,
            think=think,
            temperature=temperature,
            num_ctx=num_ctx,
            max_output_tokens=max_output_tokens,
            timeout=timeout,
        )

    def resolve_user_model(self, model_id: str) -> Optional[ResolvedModel]:
        """按 id 取「允许用户选择」的模型；不存在 / 不可选 / 非 chat → None。

        三个条件缺一不可：
        - 模型必须存在（前端传来的 id 不可信）
        - `user_selectable`（默认 False，见迁移 a7b8c9d0e1f2）
        - 具备 `chat` 能力——embedding 模型即便被误标可选也不该出现在对话选择里

        校验放服务层而不是 API 层：非流式 `run()` 与流式 `arun_stream()` 两条路径
        都要过这道门，放在接口层就会漏一条。
        """
        if not model_id:
            return None
        try:
            resolved = self._get_registry().get_model(model_id)
        except Exception:
            return None
        model = resolved.model
        if not getattr(model, "user_selectable", False) or not model.has("chat"):
            return None
        return resolved

    def model_thinking_control(self, model_id: Optional[str]) -> Optional[str]:
        """指定模型的思考可控性：off / switchable / always_on；查不到返回 None。

        为什么不看 `settings.model.OLLAMA_SUPPORTS_THINKING`：那是「这个部署能接
        思考参数吗」的总闸，既不区分模型、也不说明「关不关得掉」。同一 provider 下
        qwen3 会思考、qwen2.5 完全不会；而 qwen3 在 LM Studio 上又关不掉——
        这几种情况在界面上必须是三种不同的表现，不能被一个全局布尔压平。
        """
        if not model_id:
            return None
        try:
            resolved = self._get_registry().get_model(model_id)
        except Exception:
            return None
        return resolve_thinking_control(resolved.model, resolved.provider)

    def same_tier_task(
        self,
        selected: ResolvedModel,
        *,
        think: Optional[bool] = None,
        default_task: str = "answer",
    ) -> str:
        """与所选模型**同档位**的那条路由名，用来决定「挂了让谁接手」。

        「同档位」的意思是：直答档兜直答档，会思考档兜会思考档。

        为什么要按档位而不是「把默认路由整条塞进来」：那样会出现
        「用户选了直答、主模型抖了一下、最后答出来的是一个会思考的慢答案」，
        或反过来「选了会思考却被静默换成直答」——等于在用户不知情时换掉了档位，
        而档位正是这个界面上唯一的选择语义。

        `think` 优先于档位：可切换的模型（`switchable`）在开关关掉时，这一轮
        实际就是直答，兜底也该找直答的模型。
        """
        if think is not None:
            return "answer_direct" if think is False else "answer"

        control = resolve_thinking_control(selected.model, selected.provider)
        if control == THINKING_OFF:
            return "answer_direct"
        if control in (THINKING_SWITCHABLE, THINKING_ALWAYS_ON):
            return "answer"
        # 档位判不出来（老配置/未知 provider）：退回调用方给的 task，不硬编一个
        return default_task

    def build_user_model_group(
        self,
        model_id: str,
        *,
        task: str = "answer",
        streaming: bool = True,
        think: Optional[bool] = None,
        temperature: Optional[float] = None,
    ) -> Any:
        """按用户选择的模型构造模型组；不可选时返回 None（调用方回落默认）。

        兜底只在**同档位**内找（见 `same_tier_task`）：选了 A、A 的服务挂了，
        「什么都不答」比「换个同档位的答出来并告知」更糟，但「换个别的档位答出来」
        更糟——那不是降级，是替用户改了选择。同档位没有候选时不兜底，
        由上层报错并告知，宁可不答也不换档。

        ChatModelGroup 会按顺序尝试，且每个 target 各自记用量与熔断，
        所以成本归因不会串到兜底的模型上。
        """
        selected = self.resolve_user_model(model_id)
        if selected is None:
            return None

        fallbacks: List[ResolvedModel] = []
        try:
            route = self._get_registry().resolve(
                self.same_tier_task(selected, think=think, default_task=task)
            )
            for item in [route.primary, *route.fallbacks]:
                if item.model.id != selected.model.id:
                    fallbacks.append(item)
        except Exception:
            # 同档位路由不可用不该让「用户选择」整体失效，只是没有兜底
            pass

        resolved_route = ResolvedRoute(
            task=task,
            primary=selected,
            fallbacks=fallbacks,
            config=RouteConfig(
                primary=selected.model.id,
                fallbacks=[item.model.id for item in fallbacks],
            ),
            source="user-selection",
        )
        return build_route_models(
            resolved_route,
            streaming=streaming,
            think=think,
            temperature=temperature,
        )

    @property
    def embedding_model_key(self) -> str:
        """当前 embedding 路由的稳定标识（用于缓存失效）。"""
        resolved = self._get_registry().get_embedding_model()
        return f"{resolved.provider.id}:{resolved.model.id}:{resolved.model.model}"

    def get_embeddings(self):
        """按 embedding route 返回共享 Embedding 实例（按 provider/model 缓存）。"""
        registry = self._get_registry()
        resolved = registry.get_embedding_model()
        cache_key = f"{resolved.provider.id}:{resolved.model.id}:{resolved.model.model}"
        if cache_key not in self._embeddings_cache:
            self._embeddings_cache[cache_key] = build_embeddings(resolved)
        return self._embeddings_cache[cache_key]

    def get_reranker_model(self) -> Optional[ResolvedModel]:
        """可选的 rerank 路由；未配置时返回 None，回退 env 的 KBReranker 配置。"""
        registry = self._get_registry()
        route = registry.routes.get("rerank")
        if route is None:
            return None
        try:
            resolved = registry.get_model(route.primary)
        except Exception as exc:
            # 路由指向的模型被删除/停用时静默关闭 rerank，需可见否则质量悄悄下降
            log_exception(
                logger, exc,
                action="model_manager.rerank_route",
                severity=ErrorSeverity.RECOVERABLE,
                message="rerank 路由目标模型不可解析，本次不启用 rerank",
                primary=route.primary,
            )
            return None
        if not resolved.provider.enabled or not resolved.model.has("rerank"):
            return None
        return resolved

    async def describe_route(self, task: str) -> Dict[str, Any]:
        """返回任务路由的可展示信息（管理 UI / 诊断用）。"""
        registry = self._get_registry()
        route = registry.resolve(task)
        return {
            "task": task,
            "source": route.source,
            "primary": {
                "id": route.primary.model.id,
                "display_name": route.primary.model.display_name,
                "provider": route.primary.provider.id,
                "provider_type": route.primary.provider.type,
                "model": route.primary.model.model,
                "capabilities": route.primary.model.capabilities,
                "external": route.primary.provider.is_external(),
            },
            "fallbacks": [
                {
                    "id": item.model.id,
                    "provider": item.provider.id,
                    "model": item.model.model,
                }
                for item in route.fallbacks
            ],
        }

    def assert_configured(self) -> None:
        """启动/管理校验入口：加载并验证注册表。"""
        self._get_registry()


# 全局默认模型管理器
model_manager = ModelManager()
