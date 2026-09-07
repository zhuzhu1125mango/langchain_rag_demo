"""混合检索辅助模块。

提供多路检索结果的 RRF（Reciprocal Rank Fusion）融合与 Cross-Encoder 重排序能力。
本模块设计为与 MilvusService、WebSearchService 解耦，可被知识库检索与联网搜索共同复用。
"""

import asyncio
import logging
from typing import Dict, List, Optional

from src.config import settings

logger = logging.getLogger("hybrid_search")


def _result_key(result: Dict) -> str:
    """生成结果去重键：同一文档同一 chunk 视为同一条结果。"""
    doc_id = result.get("document_id") or result.get("id") or ""
    chunk_idx = result.get("chunk_index", 0)
    return f"{doc_id}::{chunk_idx}"


def reciprocal_rank_fusion(
    channel_results: Dict[str, List[Dict]],
    k: int = 60,
) -> List[Dict]:
    """对多路检索结果做 Reciprocal Rank Fusion。

    每路结果按排名赋予分数：score = 1 / (k + rank)，rank 从 1 开始。
    同一 chunk 在多个通道中出现时，分数累加；保留原始通道分数用于后续分析。

    Args:
        channel_results: 通道名称到结果列表的映射，例如
            {"dense": [...], "sparse": [...]}。
        k: RRF 平滑因子，默认 60。

    Returns:
        按融合分降序排列的结果列表，每个结果额外包含：
        - rrf_score: 融合后的总分
        - dense_score: dense 通道原始分数（如有）
        - sparse_score: sparse 通道原始分数（如有）
    """
    fused = {}

    for channel_name, results in channel_results.items():
        for rank, result in enumerate(results, start=1):
            key = _result_key(result)
            if key not in fused:
                fused[key] = {
                    **result,
                    "rrf_score": 0.0,
                    "dense_score": 0.0,
                    "sparse_score": 0.0,
                }
            rrf_score = 1.0 / (k + rank)
            fused[key]["rrf_score"] += rrf_score
            if channel_name == "dense":
                fused[key]["dense_score"] = result.get("score", 0.0)
            elif channel_name == "sparse":
                fused[key]["sparse_score"] = result.get("score", 0.0)

    ranked = sorted(fused.values(), key=lambda x: x["rrf_score"], reverse=True)
    return ranked


class KBReranker:
    """基于 Cross-Encoder 或 Ollama 的知识库结果重排序器。

    模型加载失败或不可用时，按输入顺序（RRF 分数）直接截断返回，不阻塞主流程。
    """

    _instance = None
    _lock = asyncio.Lock()
    _model = None
    _ollama_reranker = None
    _model_loaded = False
    _model_failed = False

    def __init__(self, model_name: Optional[str] = None, provider: Optional[str] = None):
        self.model_name = model_name or settings.processing.KB_RERANK_MODEL
        self.provider = (provider or settings.processing.KB_RERANK_PROVIDER).lower()

    @classmethod
    def _enabled(cls) -> bool:
        """rerank 是否可用（C1：开关关闭或未配置模型时不加载模型）。"""
        return bool(settings.processing.KB_RERANK_ENABLED and settings.processing.KB_RERANK_MODEL)

    @classmethod
    def model_loaded(cls) -> bool:
        """模型是否已成功加载（C8：此时 rerank_score 才是真实相关性分数，
        可用于阈值过滤；未加载时 rerank_score 为 RRF 排序分，不可作相关性依据）。"""
        return cls._model_loaded

    @classmethod
    async def get_instance(cls) -> "KBReranker":
        """获取重排序器单例，避免重复加载模型。"""
        if cls._instance is None:
            cls._instance = cls()
        if not cls._enabled():
            # rerank 关闭：不加载模型，rerank() 走 RRF 截断路径
            return cls._instance
        if not cls._model_loaded and not cls._model_failed:
            await cls._instance._load_model()
        return cls._instance

    async def _load_model(self):
        """异步加载重排序模型。"""
        async with KBReranker._lock:
            if KBReranker._model_loaded or KBReranker._model_failed:
                return
            if self.provider == "ollama":
                try:
                    from src.services.ollama_reranker import OllamaReranker

                    logger.info(f"正在加载 Ollama 知识库重排序模型: {self.model_name}")
                    KBReranker._ollama_reranker = OllamaReranker(self.model_name)
                    KBReranker._model_loaded = True
                    logger.info("Ollama 知识库重排序模型加载完成")
                except Exception as e:
                    logger.warning(f"Ollama 知识库重排序模型加载失败，将按 RRF 分数直接截断: {e}")
                    KBReranker._model_failed = True
                    KBReranker._ollama_reranker = None
                return
            try:
                from sentence_transformers import CrossEncoder

                logger.info(f"正在加载知识库重排序模型: {self.model_name}")
                KBReranker._model = await asyncio.to_thread(CrossEncoder, self.model_name)
                KBReranker._model_loaded = True
                logger.info("知识库重排序模型加载完成")
            except Exception as e:
                logger.warning(f"知识库重排序模型加载失败，将按 RRF 分数直接截断: {e}")
                KBReranker._model_failed = True
                KBReranker._model = None

    async def rerank(self, query: str, results: List[Dict], top_k: int = 5) -> List[Dict]:
        """对候选结果重排序。

        Args:
            query: 原始查询文本。
            results: 候选结果列表，每个元素至少包含 content / score / rrf_score。
            top_k: 返回结果数量上限。

        Returns:
            重排序后的结果列表，每个结果额外包含 `rerank_score`。
            若模型不可用，返回按 rrf_score 排序的前 top_k 个结果，rerank_score 等于 rrf_score。
        """
        if not results:
            return []

        if not KBReranker._model_loaded:
            sorted_results = sorted(results, key=lambda x: x.get("rrf_score", 0.0), reverse=True)
            return [dict(r, rerank_score=r.get("rrf_score", 0.0)) for r in sorted_results[:top_k]]

        try:
            pairs = [[query, r.get("content", "")] for r in results]
            if self.provider == "ollama" and KBReranker._ollama_reranker is not None:
                scores = await KBReranker._ollama_reranker.predict(pairs)
            else:
                scores = await asyncio.to_thread(KBReranker._model.predict, pairs)

            for result, score in zip(results, scores):
                result["rerank_score"] = float(score)

            min_score = settings.processing.KB_RERANK_MIN_SCORE
            ranked = sorted(
                [r for r in results if r["rerank_score"] >= min_score],
                key=lambda x: x["rerank_score"],
                reverse=True,
            )
            return ranked[:top_k]
        except Exception as e:
            logger.warning(f"知识库重排序失败，降级为 RRF 截断: {e}")
            sorted_results = sorted(results, key=lambda x: x.get("rrf_score", 0.0), reverse=True)
            return [dict(r, rerank_score=r.get("rrf_score", 0.0)) for r in sorted_results[:top_k]]


async def rerank_results(query: str, results: List[Dict], top_k: int = 5) -> List[Dict]:
    """便捷函数：获取重排序器实例并执行重排序。"""
    reranker = await KBReranker.get_instance()
    return await reranker.rerank(query, results, top_k=top_k)
