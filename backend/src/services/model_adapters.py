"""模型 Provider 适配器（P1）。

- Ollama：保留 think / num_ctx / num_gpu 语义
- OpenAI-Compatible：覆盖 OpenAI、DeepSeek、通义兼容、Kimi、vLLM、LM Studio 等

业务层只拿 LangChain 兼容对象；不要在这里读取业务配置或直接访问数据库。
"""

from __future__ import annotations

import logging
import time
from typing import Any, AsyncIterator, List, Optional

from src.config import settings
from src.services.model_health import ProviderCircuitError, provider_health
from src.services.model_registry import (
    ModelRegistryError,
    ResolvedModel,
    ResolvedRoute,
    _normalize_ollama_base_url,
)
from src.services.model_usage import model_usage
from src.utils.error_handling import ErrorSeverity, log_exception

logger = logging.getLogger("rag_system")


class ChatModelGroup:
    """带跨 Provider fallback、熔断与用量统计的聊天模型组。

    - 主模型失败时按顺序尝试 fallback；熔断中的 Provider 直接跳过
    - 每次调用记录 Provider/模型/任务的延迟、Token 与失败
    - 显式实现 ``bind_tools``，保证 Agent 决策链路仍可用
    - ``targets`` 为空（如单测直接构造）时不启用健康检查与用量统计
    """

    def __init__(
        self,
        models: List[Any],
        labels: Optional[List[str]] = None,
        targets: Optional[List[ResolvedModel]] = None,
        task: str = "",
    ):
        if not models:
            raise ModelRegistryError("ChatModelGroup 至少需要一个模型")
        self.models = models
        self.labels = labels or [f"model-{i}" for i in range(len(models))]
        self.targets = targets or []
        self.task = task or "chat"

    def _meta(self, index: int) -> Optional[tuple[str, str, str]]:
        if index >= len(self.targets):
            return None
        target = self.targets[index]
        return target.provider.id, target.model.id, target.model.model

    def _enter(self, index: int) -> bool:
        meta = self._meta(index)
        if meta is None:
            return True
        provider_id = meta[0]
        if provider_health.is_available(provider_id):
            return True
        logger.warning("Provider 熔断中，跳过: task=%s, provider=%s", self.task, provider_id)
        return False

    def _success(self, index: int, started: float, usage: tuple[int, int] = (0, 0)) -> None:
        meta = self._meta(index)
        if meta is None:
            return
        provider_id, model_id, model_name = meta
        model_config = getattr(self.targets[index], "model", None)
        provider_health.record_success(provider_id)
        model_usage.record(
            provider_id=provider_id,
            model_id=model_id,
            model_name=model_name,
            task=self.task,
            latency_ms=(time.perf_counter() - started) * 1000,
            success=True,
            input_tokens=usage[0],
            output_tokens=usage[1],
            input_cost_per_1k=getattr(model_config, "input_cost_per_1k", None),
            output_cost_per_1k=getattr(model_config, "output_cost_per_1k", None),
            currency=getattr(model_config, "price_currency", "USD") or "USD",
        )

    def _failure(self, index: int, started: float, exc: BaseException) -> None:
        meta = self._meta(index)
        if meta is None:
            return
        provider_id, model_id, model_name = meta
        provider_health.record_failure(provider_id, exc)
        model_usage.record(
            provider_id=provider_id,
            model_id=model_id,
            model_name=model_name,
            task=self.task,
            latency_ms=(time.perf_counter() - started) * 1000,
            success=False,
            error=f"{type(exc).__name__}: {exc}",
        )

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ChatModelGroup":
        return ChatModelGroup(
            [model.bind_tools(tools, **kwargs) for model in self.models],
            self.labels,
            self.targets,
            self.task,
        )

    def bind(self, **kwargs: Any) -> "ChatModelGroup":
        return ChatModelGroup(
            [model.bind(**kwargs) for model in self.models],
            self.labels,
            self.targets,
            self.task,
        )

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        last_exc: Optional[BaseException] = None
        for index, model in enumerate(self.models):
            if not self._enter(index):
                if last_exc is None:
                    last_exc = ProviderCircuitError(f"provider 熔断: {self.labels[index]}")
                continue
            started = time.perf_counter()
            try:
                result = await model.ainvoke(*args, **kwargs)
            except Exception as exc:
                self._failure(index, started, exc)
                last_exc = exc
                log_exception(
                    logger, exc,
                    action="model.route.ainvoke",
                    severity=ErrorSeverity.RECOVERABLE,
                    message="模型调用失败，尝试 fallback",
                    task=self.task, model=self.labels[index],
                )
                continue
            self._success(index, started, _token_usage(result))
            return result
        if last_exc is not None:
            raise last_exc
        raise ModelRegistryError(f"task {self.task} 无可用模型")

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        last_exc: Optional[BaseException] = None
        for index, model in enumerate(self.models):
            if not self._enter(index):
                if last_exc is None:
                    last_exc = ProviderCircuitError(f"provider 熔断: {self.labels[index]}")
                continue
            started = time.perf_counter()
            try:
                result = model.invoke(*args, **kwargs)
            except Exception as exc:
                self._failure(index, started, exc)
                last_exc = exc
                log_exception(
                    logger, exc,
                    action="model.route.invoke",
                    severity=ErrorSeverity.RECOVERABLE,
                    message="模型调用失败，尝试 fallback",
                    task=self.task, model=self.labels[index],
                )
                continue
            self._success(index, started, _token_usage(result))
            return result
        if last_exc is not None:
            raise last_exc
        raise ModelRegistryError(f"task {self.task} 无可用模型")

    async def astream(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        last_exc: Optional[BaseException] = None
        for index, model in enumerate(self.models):
            if not self._enter(index):
                if last_exc is None:
                    last_exc = ProviderCircuitError(f"provider 熔断: {self.labels[index]}")
                continue
            started = time.perf_counter()
            yielded = False
            input_tokens, output_tokens = 0, 0
            try:
                async for chunk in model.astream(*args, **kwargs):
                    yielded = True
                    chunk_in, chunk_out = _token_usage(chunk)
                    input_tokens = max(input_tokens, chunk_in)
                    output_tokens = max(output_tokens, chunk_out)
                    yield chunk
            except Exception as exc:
                self._failure(index, started, exc)
                if yielded:
                    # 已经向客户端输出内容，不能再切换模型：本次回答必然被截断
                    log_exception(
                        logger, exc,
                        action="model.route.astream.interrupted",
                        severity=ErrorSeverity.REPORTABLE,
                        message="流式输出中途中断，已下发内容无法回退",
                        task=self.task, model=self.labels[index],
                    )
                    raise
                last_exc = exc
                log_exception(
                    logger, exc,
                    action="model.route.astream",
                    severity=ErrorSeverity.RECOVERABLE,
                    message="模型流式调用失败，尝试 fallback",
                    task=self.task, model=self.labels[index],
                )
                continue
            self._success(index, started, (input_tokens, output_tokens))
            return
        if last_exc is not None:
            raise last_exc
        raise ModelRegistryError(f"task {self.task} 无可用模型")

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        last_exc: Optional[BaseException] = None
        for index, model in enumerate(self.models):
            if not self._enter(index):
                if last_exc is None:
                    last_exc = ProviderCircuitError(f"provider 熔断: {self.labels[index]}")
                continue
            started = time.perf_counter()
            yielded = False
            input_tokens, output_tokens = 0, 0
            try:
                for chunk in model.stream(*args, **kwargs):
                    yielded = True
                    chunk_in, chunk_out = _token_usage(chunk)
                    input_tokens = max(input_tokens, chunk_in)
                    output_tokens = max(output_tokens, chunk_out)
                    yield chunk
            except Exception as exc:
                self._failure(index, started, exc)
                if yielded:
                    # 同上：已产出分片，切换模型会导致内容错乱
                    log_exception(
                        logger, exc,
                        action="model.route.stream.interrupted",
                        severity=ErrorSeverity.REPORTABLE,
                        message="流式输出中途中断，已下发内容无法回退",
                        task=self.task, model=self.labels[index],
                    )
                    raise
                last_exc = exc
                log_exception(
                    logger, exc,
                    action="model.route.stream",
                    severity=ErrorSeverity.RECOVERABLE,
                    message="模型流式调用失败，尝试 fallback",
                    task=self.task, model=self.labels[index],
                )
                continue
            self._success(index, started, (input_tokens, output_tokens))
            return
        if last_exc is not None:
            raise last_exc
        raise ModelRegistryError(f"task {self.task} 无可用模型")


def _token_usage(message: Any) -> tuple[int, int]:
    """尽量从消息/流式分片中提取 input/output token 数。"""
    usage = getattr(message, "usage_metadata", None) or {}
    response_metadata = getattr(message, "response_metadata", None) or {}
    input_tokens = usage.get("input_tokens") or response_metadata.get("prompt_eval_count") or 0
    output_tokens = usage.get("output_tokens") or response_metadata.get("eval_count") or 0
    try:
        return int(input_tokens), int(output_tokens)
    except (TypeError, ValueError):
        return 0, 0


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
    return ChatModelGroup(models, labels, candidates, route.task)


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
