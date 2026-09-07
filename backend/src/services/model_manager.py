"""模型分工管理器。

负责任务级别模型选择、可用性检查与自动降级，
确保各任务（回答生成、快速推理、Embedding、意图路由等）使用合适的本地模型。
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from src.config import settings

logger = logging.getLogger(__name__)


@dataclass
class ModelInfo:
    """模型信息。"""

    name: str
    task: str
    available: bool
    error: Optional[str] = None


class ModelManager:
    """本地模型管理器。

    维护任务到模型名称的映射，并提供可用性检查与自动降级。
    当首选模型不可用时，按 fallback 链顺序返回第一个可用模型。
    """

    # 默认任务角色映射（可通过配置覆盖）
    DEFAULT_TASK_ROLES: Dict[str, str] = {
        "answer": "OLLAMA_MODEL_NAME",
        "fast": "FAST_LLM_MODEL_NAME",
        "embedding": "EMBEDDING_MODEL_NAME",
        "intent_router": "FAST_LLM_MODEL_NAME",
        "title_generation": "FAST_LLM_MODEL_NAME",
        "query_rewrite": "FAST_LLM_MODEL_NAME",
    }

    # 各任务的降级链：当前模型不可用时依次尝试
    DEFAULT_FALLBACK_CHAINS: Dict[str, List[str]] = {
        "answer": ["OLLAMA_MODEL_NAME", "FAST_LLM_MODEL_NAME"],
        "fast": ["FAST_LLM_MODEL_NAME", "OLLAMA_MODEL_NAME"],
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
        self.task_roles = task_roles or self._load_task_roles_from_settings()
        self.fallback_chains = fallback_chains or self.DEFAULT_FALLBACK_CHAINS
        self.ollama_host = ollama_host or settings.model.OLLAMA_HOST
        self._availability_cache: Dict[str, bool] = {}
        self._cache_lock = asyncio.Lock()
        # 共享 OllamaEmbeddings 单例缓存（键为模型名，B2）
        self._embeddings_cache: Dict[str, object] = {}

    @classmethod
    def _load_task_roles_from_settings(cls) -> Dict[str, str]:
        """从 settings 中加载任务角色配置（若存在）。"""
        configured = getattr(settings, "model_task_roles", None)
        if isinstance(configured, dict):
            return {k: v for k, v in configured.items()}
        return dict(cls.DEFAULT_TASK_ROLES)

    def _resolve_model_name(self, model_ref: str) -> str:
        """将模型引用（如 OLLAMA_MODEL_NAME）解析为实际模型名。"""
        if hasattr(settings.model, model_ref):
            return getattr(settings.model, model_ref)
        return model_ref

    async def check_availability(self, model_name: str) -> bool:
        """异步检查指定模型是否已在本地 Ollama 中可用。

        使用 ollama.list() 获取本地模型列表，结果缓存避免重复调用。
        若 ollama 不可用，返回 True（不阻塞主流程，由调用方处理实际错误）。
        """
        if model_name in self._availability_cache:
            return self._availability_cache[model_name]

        try:
            import ollama

            async with self._cache_lock:
                if model_name in self._availability_cache:
                    return self._availability_cache[model_name]

                client = ollama.Client(host=self.ollama_host) if self.ollama_host else ollama.Client()
                # ollama.list() 是同步 HTTP 调用，使用 to_thread 避免阻塞
                response = await asyncio.to_thread(client.list)
                models = response.get("models", [])
                available_names = {m.get("name", "") for m in models}
                # 兼容不同 ollama 版本返回的字段
                available_names.update(m.get("model", "") for m in models)

                is_available = model_name in available_names
                self._availability_cache[model_name] = is_available
                if not is_available:
                    logger.warning(f"模型 {model_name} 未在本地 Ollama 中找到")
                return is_available
        except Exception as e:
            logger.warning(f"检查模型可用性失败: {e}")
            # 无法连接 ollama 时不阻塞，假设可用
            return True

    def clear_availability_cache(self) -> None:
        """清空可用性缓存。"""
        self._availability_cache.clear()

    async def get_model_for_task(
        self,
        task: str,
        preferred: Optional[str] = None,
    ) -> str:
        """获取指定任务应使用的模型名称。

        流程：
        1. 若传入 preferred，优先尝试。
        2. 按任务 fallback 链依次检查可用性。
        3. 全部不可用时返回链中第一个模型名（调用方自行处理错误）。

        Args:
            task: 任务名称，如 answer/fast/intent_router 等。
            preferred: 优先使用的模型名（可选）。

        Returns:
            可用的模型名称。
        """
        candidates: List[str] = []
        if preferred:
            candidates.append(preferred)

        chain = self.fallback_chains.get(task, [])
        for ref in chain:
            candidates.append(self._resolve_model_name(ref))

        if not candidates:
            # 无配置时回退到主模型
            return settings.model.OLLAMA_MODEL_NAME

        # 去重并保持顺序
        seen = set()
        unique_candidates = []
        for name in candidates:
            if name and name not in seen:
                seen.add(name)
                unique_candidates.append(name)

        for name in unique_candidates:
            try:
                if await self.check_availability(name):
                    if name != unique_candidates[0]:
                        logger.info(f"任务 {task} 首选模型不可用，降级为 {name}")
                    return name
            except Exception as e:
                logger.warning(f"检查模型 {name} 可用性失败: {e}")
                continue

        # 全部不可用，返回第一个候选，由调用方报错
        fallback_name = unique_candidates[0]
        logger.warning(f"任务 {task} 所有候选模型均不可用，返回 {fallback_name}")
        return fallback_name

    async def get_available_models(self) -> List[ModelInfo]:
        """获取所有任务角色的模型可用性列表。"""
        result = []
        for task, model_ref in self.task_roles.items():
            model_name = self._resolve_model_name(model_ref)
            try:
                available = await self.check_availability(model_name)
                result.append(ModelInfo(name=model_name, task=task, available=available))
            except Exception as e:
                result.append(ModelInfo(name=model_name, task=task, available=False, error=str(e)))
        return result

    async def get_chat_llm(
        self,
        task: str,
        *,
        streaming: bool = False,
        think: bool = False,
        num_ctx: Optional[int] = None,
        temperature: Optional[float] = None,
        timeout: Optional[float] = None,
        preferred: Optional[str] = None,
    ):
        """按任务角色构造 ChatOllama 实例（B1 统一模型工厂）。

        模型名走现有 task role + fallback 链解析；统一注入 num_ctx；
        think 显式绑定 reasoning（与主回答链写法一致）——OLLAMA_SUPPORTS_THINKING=true
        时按 think 值绑定，避免混合思考模型在结构化小任务上空烧思考链；
        辅助任务一律 think=False，主回答任务由 should_think() 结果传入。

        Args:
            task: 任务名称（answer/fast/intent_router/title_generation/query_rewrite 等）。
            streaming: 是否流式。
            think: 是否绑定 reasoning=think（None 表示不干预，用模型默认行为）。
            num_ctx: 上下文窗口，None 时使用 OLLAMA_NUM_CTX。
            temperature: 采样温度，None 时使用 ChatOllama 默认值。
            timeout: 请求超时（秒），None 时不设置。
            preferred: 优先使用的模型名（可选，优先于任务 fallback 链）。

        Returns:
            ChatOllama（或绑定 reasoning 后的 Runnable）实例。
        """
        from langchain_ollama import ChatOllama

        model_name = await self.get_model_for_task(task, preferred=preferred)
        kwargs = {
            "model": model_name,
            "streaming": streaming,
            "num_ctx": num_ctx if num_ctx is not None else settings.model.OLLAMA_NUM_CTX,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if timeout is not None:
            kwargs["timeout"] = timeout
        llm = ChatOllama(**kwargs)
        if think is not None and settings.model.OLLAMA_SUPPORTS_THINKING:
            return llm.bind(reasoning=think)
        return llm

    def get_embeddings(self):
        """获取共享 OllamaEmbeddings 单例（B2，按模型名缓存，模型名变化时重建）。

        Embedding 任务在 fallback 链中只有单一模型（启动校验强制其存在），
        因此同步构造即可；业务侧避免各自重复创建实例。
        """
        from langchain_ollama import OllamaEmbeddings

        model_ref = self.task_roles.get("embedding", "EMBEDDING_MODEL_NAME")
        model_name = self._resolve_model_name(model_ref)
        if model_name not in self._embeddings_cache:
            self._embeddings_cache[model_name] = OllamaEmbeddings(model=model_name)
        return self._embeddings_cache[model_name]


# 全局默认模型管理器
model_manager = ModelManager()
