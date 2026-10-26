"""模型 Provider 适配器（P1）。

- Ollama：保留 think / num_ctx / num_gpu 语义
- OpenAI-Compatible：覆盖 OpenAI、DeepSeek、通义兼容、Kimi、vLLM、LM Studio 等

业务层只拿 LangChain 兼容对象；不要在这里读取业务配置或直接访问数据库。
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, List, Optional

from src.config import settings
from src.services.model_registry import (
    ModelRegistryError,
    ResolvedModel,
    ResolvedRoute,
    _normalize_ollama_base_url,
)

logger = logging.getLogger("rag_system")


class ChatModelGroup:
    """带跨 Provider fallback 的聊天模型组。

    - 单模型时直接保留原生模型对象，零额外开销
    - 多模型时用 LangChain ``with_fallbacks``
    - 显式实现 ``bind_tools``，保证 Agent 决策链路仍可用
    """

    def __init__(self, models: List[Any], labels: Optional[List[str]] = None):
        if not models:
            raise ModelRegistryError("ChatModelGroup 至少需要一个模型")
        self.models = models
        self.labels = labels or [f"model-{i}" for i in range(len(models))]
        self._runnable = models[0].with_fallbacks(models[1:]) if len(models) > 1 else models[0]

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ChatModelGroup":
        return ChatModelGroup(
            [model.bind_tools(tools, **kwargs) for model in self.models],
            self.labels,
        )

    def bind(self, **kwargs: Any) -> "ChatModelGroup":
        return ChatModelGroup([model.bind(**kwargs) for model in self.models], self.labels)

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        return await self._runnable.ainvoke(*args, **kwargs)

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        return self._runnable.invoke(*args, **kwargs)

    async def astream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        async for chunk in self._runnable.astream(*args, **kwargs):
            yield chunk

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        return self._runnable.stream(*args, **kwargs)


def _ollama_base_url(provider: Any) -> Optional[str]:
    return provider.base_url or _normalize_ollama_base_url(settings.model.OLLAMA_HOST)


def _resolved_api_key(provider: Any) -> Optional[str]:
    key = provider.resolved_api_key()
    if key:
        return key
    if provider.api_key_required:
        env_hint = f"（环境变量 {provider.api_key_env}）" if provider.api_key_env else ""
        raise ModelRegistryError(f"provider {provider.id} 缺少 API Key{env_hint}")
    # vLLM / LM Studio 等本地 OpenAI-Compatible 服务通常不校验 Key
    return "not-needed"


def build_chat_model(
    resolved: ResolvedModel,
    *,
    streaming: bool,
    think: Optional[bool],
    temperature: Optional[float] = None,
    num_ctx: Optional[int] = None,
    max_output_tokens: Optional[int] = None,
    timeout: Optional[float] = None,
) -> Any:
    """按 ResolvedModel 构造一个 LangChain ChatModel。"""
    model = resolved.model
    provider = resolved.provider
    effective_temperature = temperature if temperature is not None else model.temperature
    effective_max_tokens = max_output_tokens or model.max_output_tokens or None

    if provider.type == "ollama":
        from langchain_ollama import ChatOllama

        kwargs: dict[str, Any] = {
            "model": model.model,
            "streaming": streaming,
            "num_ctx": num_ctx or model.context_window or settings.model.OLLAMA_NUM_CTX,
        }
        base_url = _ollama_base_url(provider)
        if base_url:
            kwargs["base_url"] = base_url
        if effective_temperature is not None:
            kwargs["temperature"] = effective_temperature
        if effective_max_tokens:
            kwargs["num_predict"] = effective_max_tokens
        llm = ChatOllama(**kwargs)
        if (
            think is not None
            and model.has("reasoning")
            and settings.model.OLLAMA_SUPPORTS_THINKING
        ):
            llm = llm.bind(reasoning=think)
        return llm

    if provider.type == "openai_compatible":
        from langchain_openai import ChatOpenAI

        kwargs = {
            "model": model.model,
            "base_url": provider.base_url,
            "api_key": _resolved_api_key(provider),
            "streaming": streaming,
            "timeout": timeout or provider.timeout_s,
        }
        if provider.headers:
            kwargs["default_headers"] = provider.headers
        if effective_temperature is not None:
            kwargs["temperature"] = effective_temperature
        if effective_max_tokens:
            # OpenAI o 系列使用 max_completion_tokens；普通 OpenAI-Compatible
            # （DeepSeek/Qwen/vLLM 等）通过 model_kwargs 发送传统 max_tokens。
            if model.model.lower().startswith(("o1", "o3", "o4")):
                kwargs["max_completion_tokens"] = effective_max_tokens
            else:
                kwargs["max_tokens"] = effective_max_tokens
        # OpenAI o 系列等显式支持 reasoning_effort 的模型才传；普通兼容 API 不传，
        # 避免 DeepSeek/Qwen/vLLM 因未知参数 400。
        if think is not None and model.has("reasoning") and model.model.lower().startswith(("o1", "o3", "o4")):
            kwargs["reasoning_effort"] = "medium" if think else "low"
        return ChatOpenAI(**kwargs)

    raise ModelRegistryError(f"不支持的 provider.type: {provider.type}")


def build_route_models(
    route: ResolvedRoute,
    *,
    streaming: bool,
    think: Optional[bool],
    temperature: Optional[float] = None,
    num_ctx: Optional[int] = None,
    max_output_tokens: Optional[int] = None,
    timeout: Optional[float] = None,
) -> ChatModelGroup:
    """构造主模型 + fallbacks 的模型组。"""
    route_config = route.config
    route_temperature = temperature if temperature is not None else (route_config.temperature if route_config else None)
    route_max_tokens = max_output_tokens or (route_config.max_output_tokens if route_config else None)
    route_timeout = timeout or (route_config.timeout_s if route_config else None)

    # 同一 provider + 同一模型名只保留一次，避免 legacy 路由里
    # answer/direct/fast 指向同一 Ollama 模型时生成无意义的重复 fallback。
    candidates: list[ResolvedModel] = []
    seen: set[tuple[str, str]] = set()
    for item in [route.primary, *route.fallbacks]:
        key = (item.provider.id, item.model.model)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(item)

    models = [
        build_chat_model(
            item,
            streaming=streaming,
            think=think,
            temperature=route_temperature,
            num_ctx=num_ctx,
            max_output_tokens=route_max_tokens,
            timeout=route_timeout,
        )
        for item in candidates
    ]
    labels = [f"{item.provider.id}/{item.model.id}" for item in candidates]
    return ChatModelGroup(models, labels)


def build_embeddings(resolved: ResolvedModel) -> Any:
    """构造 Embedding 适配器（P1 支持 Ollama / OpenAI-Compatible）。"""
    model = resolved.model
    provider = resolved.provider

    if provider.type == "ollama":
        from langchain_ollama import OllamaEmbeddings

        kwargs: dict[str, Any] = {"model": model.model, "num_gpu": 0}
        base_url = _ollama_base_url(provider)
        if base_url:
            kwargs["base_url"] = base_url
        return OllamaEmbeddings(**kwargs)

    if provider.type == "openai_compatible":
        from langchain_openai import OpenAIEmbeddings

        kwargs = {
            "model": model.model,
            "base_url": provider.base_url,
            "api_key": _resolved_api_key(provider),
            # 兼容非 OpenAI 的 /v1/embeddings：直接发送文本，避免 tiktoken 预分词
            "check_embedding_ctx_length": False,
        }
        if model.embedding_dimension:
            kwargs["dimensions"] = model.embedding_dimension
        return OpenAIEmbeddings(**kwargs)

    raise ModelRegistryError(f"不支持 embedding provider.type: {provider.type}")
