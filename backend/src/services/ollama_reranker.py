"""Ollama 托管的 reranker 模型适配器。

当 reranker 模型通过 Ollama 本地部署（如 qllama/bge-reranker-v2-m3）时，
本适配器通过 Ollama generate API 获取 query-doc  pair 的相关性分数。
"""

import asyncio
import logging
import re
from typing import Dict, List, Optional, Tuple

from ollama import AsyncClient

from src.config import settings
from src.utils.error_handling import ErrorSeverity, log_exception

logger = logging.getLogger("ollama_reranker")

# 打分失败节流：单次检索会并发几十条 pair，逐条记录会刷屏。
# 前 3 次逐条留痕（拿到原始异常），之后每 50 次补一条，保证长时故障仍可见。
_SCORE_LOG_EVERY = 50
_score_failures = 0
_parse_failures = 0


_DEFAULT_RERANK_PROMPT = """You are a search relevance evaluator. Rate how relevant the document is to the query.
Output only a single number from 0 to 10, where 10 means perfectly relevant.
Do not explain.

Query: {query}
Document: {document}
Relevance score (0-10):"""


class OllamaReranker:
    """基于 Ollama generate API 的轻量重排序器。

    适用于 Ollama 托管的 cross-encoder/reranker 模型。由于 Ollama 没有原生 rerank
    接口，这里通过构造打分提示词，从模型输出中解析数值分数。
    """

    _instances: Dict[str, "OllamaReranker"] = {}
    _lock = asyncio.Lock()

    def __new__(cls, model_name: str):
        if model_name not in cls._instances:
            cls._instances[model_name] = super().__new__(cls)
            cls._instances[model_name]._client = None
            cls._instances[model_name]._initialized = False
        return cls._instances[model_name]

    def __init__(self, model_name: str):
        self.model_name = model_name

    async def _ensure_initialized(self):
        """延迟初始化 Ollama 异步客户端。"""
        if self._initialized:
            return
        async with OllamaReranker._lock:
            if self._initialized:
                return
            host = getattr(settings, "OLLAMA_HOST", None)
            self._client = AsyncClient(host=host) if host else AsyncClient()
            self._initialized = True
            logger.info(f"OllamaReranker 已初始化，模型: {self.model_name}")

    @staticmethod
    def _extract_score(text: str) -> Optional[float]:
        """从模型输出中提取 0~10 范围内的数值分数。

        解析不出数字时返回 ``None`` 而不是 ``0.0``：模型没按格式输出属于
        「打分失败」，与「判定完全不相关（0 分）」是两件事。此前两者同为
        0.0，被下游 ``KB_RERANK_MIN_SCORE`` 一并过滤，表现为「知识库里没有
        相关内容」——把重排故障说成了检索结论。
        """
        text = (text or "").strip()
        if not text:
            return None

        # 优先匹配第一个看起来像数字的 token
        match = re.search(r"(\d+(?:\.\d+)?)", text)
        if not match:
            return None

        try:
            score = float(match.group(1))
        except ValueError:
            return None

        # 按常见输出范围归一化到 0~1
        if score > 10:
            # 假设模型输出的是百分制（如 75 表示 75%）
            score = max(0.0, min(100.0, score)) / 100.0
        else:
            score = max(0.0, min(10.0, score)) / 10.0
        return score

    async def _score_pair(self, query: str, document: str) -> Optional[float]:
        """对单个 query-document pair 打分；失败返回 ``None``。"""
        global _score_failures, _parse_failures
        prompt = _DEFAULT_RERANK_PROMPT.format(query=query, document=document)
        try:
            # 初始化也放进 try：客户端建不起来时每条 pair 都会失败，
            # 此前异常从 gather 冒出去后被静默转成 0.0，日志里什么都没有。
            await self._ensure_initialized()
            response = await self._client.generate(model=self.model_name, prompt=prompt)
        except Exception as exc:
            _score_failures += 1
            if _score_failures <= 3 or _score_failures % _SCORE_LOG_EVERY == 0:
                log_exception(
                    logger, exc,
                    action="rerank.ollama.score",
                    severity=ErrorSeverity.RECOVERABLE,
                    message="Ollama 重排单条打分失败，本次不计入排序",
                    model=self.model_name,
                    failures=_score_failures,
                )
            return None

        score = self._extract_score(response.get("response", ""))
        if score is None:
            _parse_failures += 1
            if _parse_failures <= 3 or _parse_failures % _SCORE_LOG_EVERY == 0:
                log_exception(
                    logger, ValueError("rerank 输出无法解析为分数"),
                    action="rerank.ollama.parse",
                    severity=ErrorSeverity.RECOVERABLE,
                    message="Ollama 重排输出未按格式给出分数，本次不计入排序",
                    model=self.model_name,
                    raw=str(response.get("response", ""))[:120],
                    parse_failures=_parse_failures,
                )
        return score

    async def predict(self, pairs: List[Tuple[str, str]]) -> List[Optional[float]]:
        """批量打分，并发执行以降低延迟。

        Args:
            pairs: [(query, document), ...]

        Returns:
            与 ``pairs`` 等长的分数列表，0~1；打分失败的条目为 ``None``，
            由调用方决定整批回退还是逐条降级——不再用 0.0 冒充低相关。
        """
        if not pairs:
            return []

        scores = await asyncio.gather(
            *[self._score_pair(q, d) for q, d in pairs],
            return_exceptions=True,
        )
        result: List[Optional[float]] = []
        for item in scores:
            if isinstance(item, BaseException):
                # _score_pair 已自行兜底，走到这里说明兜底本身出错（如 CancelledError 以外的意外）
                result.append(None)
            else:
                result.append(item)
        return result
