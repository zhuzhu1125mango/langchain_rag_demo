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
            logger.warning(f"答案校验失败，跳过警告追加: {e}")
            return None, None

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
        # 深度思考关闭时切换到非思考专用模型（think=False），避免混合模型空烧思考 token；
        # 未配置专用模型时回退主模型并绑定 reasoning=False
        if think is False and self.llm_direct is not None:
            llm = self.llm_direct
        elif think is not None:
            llm = self.llm.bind(reasoning=think)
        else:
            llm = self.llm

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

                # 记录 LLM 调用时间（Prometheus）
                if PROMETHEUS_AVAILABLE:
                    record_llm_call(settings.model.OLLAMA_MODEL_NAME, time.time() - llm_start)

                return
            except Exception as e:
                if PROMETHEUS_AVAILABLE:
                    record_llm_call_error(settings.model.OLLAMA_MODEL_NAME)
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
                    continue
                error_msg = f"模型调用失败: {str(e)}"
                yield error_msg, source_texts, source_metadata, "error"
