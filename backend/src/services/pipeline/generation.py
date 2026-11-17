"""答案生成与流式续传相关方法（自 rag_chain.py 拆出的 Mixin）。

包含：带重试的续传式流式生成、答案事实校验后缀、SSE 事件负载构造。
"""

import asyncio
import json
import logging
import time
from typing import Dict, List, Optional

from src.config import settings
from src.services.context_builder import estimate_token_count
from src.services.pipeline.state import _PipelineState
from src.utils.error_handling import log_exception

logger = logging.getLogger("rag_system")

# 导入 Prometheus 指标模块
try:
    from src.middleware.prometheus import record_llm_call, record_llm_call_error
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False


class GenerationMixin:
    """生成 / 流式续传 / 校验方法集（由 RAGChain 组装）。"""

    async def _verify_answer_suffix(
        self,
        answer: str,
        citation_sources: List[Dict],
        cross_source_data: Optional[Dict] = None,
    ) -> tuple:
        """对答案做事实校验并返回警告后缀（P0-f 链路整合）。

        不修改答案文本本身，后缀由调用方决定追加方式：
        流式在生成结束后以 chunk 追加；非流式可拼接后返回。

        Args:
            answer: 待校验的答案文本。
            citation_sources: 供校验使用的来源列表。
            cross_source_data: 多源交叉验证数据（来自 build_search_context_enhanced）。

        Returns:
            (suffix, verification_result):
                - suffix: 警告后缀（无警告时为 None）。
                - verification_result: 校验结果（校验不可用时为 None）。
        """
        if self.answer_verifier is None:
            return None, None
        try:
            verification_result = await self.answer_verifier.verify(
                answer=answer,
                sources=citation_sources,
                cross_source_data=cross_source_data,
            )
            return self.answer_verifier.format_warning_suffix(verification_result), verification_result
        except Exception as e:
            # 校验失败只是少了「答案可能有误」的提示，本轮照常返回答案
            log_exception(
                logger,
                e,
                action="answer.verify",
                message="答案校验失败，跳过警告追加",
            )
            return None, None

    async def verify_web_answer(
        self,
        answer: str,
        citation_sources: List[Dict],
        cross_source_data: Optional[Dict] = None,
    ) -> Optional[str]:
        """公开入口：对含联网来源的答案做事实校验，仅返回警告后缀（B2-3）。

        供 API 层在 end 事件后后台调用（校验为一次完整 LLM 调用，内联会阻塞
        流完成信号）。校验不可用时返回 None。
        """
        suffix, _ = await self._verify_answer_suffix(answer, citation_sources, cross_source_data)
        return suffix

    @staticmethod
    def _reasoning_payload(step: str, status: str, title: str, content: str = "", duration_ms: Optional[int] = None, metadata: Optional[Dict] = None) -> str:
        """构造 reasoning 事件负载（供 SSE 前端展示搜索/思考过程）。"""
        from src.services.reasoning import ReasoningStep
        return ReasoningStep(
            step=step,
            status=status,
            title=title,
            content=content,
            duration_ms=duration_ms,
            metadata=metadata or {},
        ).to_sse_payload()

    @staticmethod
    def _search_status_payload(status: str, message: str = "", sources: int = 0) -> str:
        """构造搜索状态事件负载（向后兼容，已映射为 reasoning 事件）。"""
        return json.dumps({"status": status, "message": message, "sources": sources})

    def _select_llm(self, *, state=None, think=None):
        """按请求选模型：用户选了就用用户的，否则走 answer / answer_direct 路由。

        为什么每次请求新建、不缓存到 `self` 上：RAGChain 是单例，把请求级模型
        写回共享字段会让并发请求互相串模型（A 用户选了模型甲，B 用户的请求跟着变）。
        构造 ChatModelGroup 只是建对象、不建连接，真正的连接与用量统计发生在调用时，
        所以这点开销可忽略。

        用户模型不可用时回落到默认路由，并把 `model_fallback` 记在 state 上：
        「静默用别的模型答」是最不该有的行为——用户以为在用 A，实际是 B。
        """
        model_id = getattr(state, "model_id", None) if state is not None else None
        if model_id:
            from src.config import settings
            from src.services.model_manager import model_manager

            group = model_manager.build_user_model_group(
                model_id,
                task="answer",
                streaming=True,
                think=think,
                temperature=settings.processing.KB_ANSWER_TEMPERATURE,
            )
            if group is not None:
                if state is not None:
                    state.resolved_model_id = model_id
                return group
            if state is not None:
                state.model_fallback = True
            logger.warning("用户选择的模型不可用，回落默认路由: model_id=%s", model_id)

        # 深度思考关闭时使用 answer_direct 路由模型；是否思考由 registry/model
        # 决定，业务层不再 bind(reasoning=...)，保持 Provider 无关。
        if think is False and self.llm_direct is not None:
            return self.llm_direct
        return self.llm

    @staticmethod
    def _record_model_outcome(state, llm) -> None:
        """把「本次回答实际由哪个模型产出」写进 state 与出口容器。

        为什么不能只靠 state.model_fallback：那个标记只覆盖「生成前就已经不可用」
        （配置被关）。真正的常见故障是**生成时**主模型调用失败，ChatModelGroup
        内部静默切到 fallback——它不会告诉调用方换了模型。不记录这件事，
        「用户选了 A、A 挂了、实际是默认模型答完」就永远不可见，
        而这恰恰是最该告诉用户的那类事实。
        """
        if state is None:
            return
        from src.services.message_payload import build_generation_info

        requested = getattr(state, "model_id", None)
        served_index = getattr(llm, "last_served_index", None)
        targets = getattr(llm, "targets", None) or []
        target = (
            targets[served_index]
            if served_index is not None and served_index < len(targets)
            else None
        )

        # 下标 0 是用户自己选的那个；>0 说明是 fallback 接手的
        fallback = bool(requested) and served_index not in (None, 0)
        state.model_fallback = state.model_fallback or fallback

        generation = None
        if target is not None:
            model = getattr(target, "model", None)
            provider = getattr(target, "provider", None)
            model_id = getattr(model, "id", "") or ""
            if model_id:
                state.resolved_model_id = model_id
                # 存快照而不是只存 id：配置改名/删除后，历史消息仍要说得清当时跑的是什么
                generation = build_generation_info(
                    model_id=model_id,
                    model_name=getattr(model, "model", "") or "",
                    display_name=getattr(model, "display_name", "") or "",
                    provider_type=getattr(provider, "type", "") or "",
                    selected_by="user" if requested else "default",
                    fallback=fallback,
                    fallback_from=requested if fallback else None,
                )

        holder = getattr(state, "model_holder", None)
        if holder is not None:
            holder["requested"] = requested
            holder["used"] = state.resolved_model_id
            holder["fallback"] = bool(state.model_fallback)
            if generation:
                holder["generation"] = generation

    async def _stream_with_retry(self, prompt, source_texts, source_metadata, answer_type, max_retries=2, state: Optional[_PipelineState] = None, think=None):
        """
        带重试机制的续传式流式生成

        中途失败重试时携带已输出内容作为续写上下文，仅产出新增部分，
        避免整体重发 prompt 导致用户看到重复内容。

        Args:
            prompt: 提示词
            source_texts: 来源文本列表
            source_metadata: 来源元信息列表
            answer_type: 回答类型
            max_retries: 最大重试次数
            state: 管线状态（可选），捕获 LLM 返回的 token 计数元数据
            think: 深度思考开关（True/False 强制，None 用模型默认）

        Yields:
            tuple: (chunk_content, source_texts, source_metadata, answer_type)
        """
        llm_start = time.time()
        yielded_part = ""
        llm = self._select_llm(state=state, think=think)

        for attempt in range(max_retries):
            current_prompt = prompt
            if yielded_part:
                current_prompt = (
                    f"{prompt}\n\n"
                    "你上一次的回答因异常中断。已输出的部分如下，"
                    "请从中断处直接继续输出剩余内容，禁止重复任何已输出内容：\n"
                    f"{yielded_part}\n\n请继续："
                )
            try:
                async for chunk in llm.astream(current_prompt):
                    # 捕获 Ollama token 计数（通常在最后一个 chunk 的元数据中）
                    if state is not None:
                        meta = getattr(chunk, "response_metadata", None) or {}
                        prompt_eval = meta.get("prompt_eval_count")
                        eval_count = meta.get("eval_count")
                        if prompt_eval or eval_count:
                            state.llm_token_meta = {
                                "prompt_tokens": prompt_eval,
                                "completion_tokens": eval_count,
                            }
                    # 模型原始思考增量（reasoning=True 时出现在 additional_kwargs）：
                    # 以 "thinking" 哨兵元组转发给 SSE，不计入续传上下文 yielded_part
                    thinking = (chunk.additional_kwargs or {}).get("reasoning_content")
                    if thinking:
                        yield thinking, source_texts, source_metadata, "thinking"
                    if chunk.content:
                        yielded_part += chunk.content
                        yield chunk.content, source_texts, source_metadata, answer_type

                # 记录 LLM 调用时间（Prometheus）；优先使用路由真实模型标识
                if PROMETHEUS_AVAILABLE:
                    labels = getattr(llm, "labels", None) or []
                    metric_model = labels[0].split("/")[-1] if labels else getattr(
                        llm, "model", settings.model.OLLAMA_MODEL_NAME
                    )
                    record_llm_call(metric_model, time.time() - llm_start)

                # 生成成功后再记录「谁答的」：组内 fallback 只有在调用发生后才知道
                self._record_model_outcome(state, llm)
                return
            except Exception as e:
                # 重试前把真实模型标识算出来，供指标与日志共用
                labels = getattr(llm, "labels", None) or []
                metric_model = labels[0].split("/")[-1] if labels else getattr(
                    llm, "model", settings.model.OLLAMA_MODEL_NAME
                )
                if PROMETHEUS_AVAILABLE:
                    record_llm_call_error(metric_model)
                # 此前这条路径只有 Prometheus 计数：指标未接时（PROMETHEUS_AVAILABLE=False）
                # 一次失败就是零痕迹，而用户看到的是「模型调用失败: xxx」被当成答案。
                is_final = attempt >= max_retries - 1
                log_exception(
                    logger,
                    e,
                    action="pipeline.generate.stream",
                    message=(
                        "LLM 流式生成失败，重试耗尽，向用户返回错误文本"
                        if is_final
                        else "LLM 流式生成失败，续传重试"
                    ),
                    attempt=attempt + 1,
                    max_retries=max_retries,
                    resumed_chars=len(yielded_part),
                    model=metric_model,
                )
                if not is_final:
                    await asyncio.sleep(1)
                    continue
                error_msg = f"模型调用失败: {str(e)}"
                yield error_msg, source_texts, source_metadata, "error"
