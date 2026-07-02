"""Ollama 托管的 reranker 模型适配器。

当 reranker 模型通过 Ollama 本地部署（如 qllama/bge-reranker-v2-m3）时，
本适配器通过 Ollama generate API 获取 query-doc  pair 的相关性分数。
"""

import asyncio
import logging
import re
from typing import Dict, List, Tuple

from ollama import AsyncClient

from src.config import settings

logger = logging.getLogger("ollama_reranker")


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
    def _extract_score(text: str) -> float:
        """从模型输出中提取 0~10 范围内的数值分数。"""
        text = text.strip()
        if not text:
            return 0.0

        # 优先匹配第一个看起来像数字的 token
        match = re.search(r"(\d+(?:\.\d+)?)", text)
        if not match:
            return 0.0

        try:
            score = float(match.group(1))
        except ValueError:
            return 0.0

        # 按常见输出范围归一化到 0~1
        if score > 10:
            # 假设模型输出的是百分制（如 75 表示 75%）
            score = max(0.0, min(100.0, score)) / 100.0
        else:
            score = max(0.0, min(10.0, score)) / 10.0
        return score

    async def _score_pair(self, query: str, document: str) -> float:
        """对单个 query-document pair 打分。"""
        await self._ensure_initialized()
        prompt = _DEFAULT_RERANK_PROMPT.format(query=query, document=document)
        try:
            response = await self._client.generate(model=self.model_name, prompt=prompt)
            return self._extract_score(response.get("response", ""))
        except Exception as e:
            logger.warning(f"Ollama rerank 单条打分失败: {e}")
            return 0.0

    async def predict(self, pairs: List[Tuple[str, str]]) -> List[float]:
        """批量打分，并发执行以降低延迟。

        Args:
            pairs: [(query, document), ...]

        Returns:
            0~1 之间的相关性分数列表。
        """
        if not pairs:
            return []

        scores = await asyncio.gather(
            *[self._score_pair(q, d) for q, d in pairs],
            return_exceptions=True,
        )
        return [
            s if isinstance(s, float) else 0.0
            for s in scores
        ]
