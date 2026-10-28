"""联网搜索正文重排：Cross-Encoder / Embedding 兜底（CODE-1 拆分）。"""

import asyncio
import logging
import threading
from typing import List, Optional


from src.config import settings
from src.services.model_manager import model_manager

logger = logging.getLogger(__name__)

from src.services.web_search_types import WebContent

class WebReranker:
    """基于 Cross-Encoder 或 Ollama 的语义重排器，主模型不可用时用 OllamaEmbeddings 兜底"""

    def __init__(self, model_name: Optional[str] = None, provider: Optional[str] = None):
        self.model_name = model_name or settings.search.SEARCH_RERANK_MODEL
        self.provider = (provider or settings.search.SEARCH_RERANK_PROVIDER).lower()
        self._model = None
        self._ollama_reranker = None
        self._embeddings = None
        self._fallback_loaded = False
        # B3：模型加载锁，防止并发请求重复加载
        self._model_load_lock = threading.Lock()

    def _load_model(self):
        """加载 Cross-Encoder 或 Ollama 重排序模型；失败时置空，由 rerank 走嵌入兜底"""
        if self.provider == "ollama" and self._ollama_reranker is None:
            try:
                from src.services.ollama_reranker import OllamaReranker

                logger.info(f"正在加载 Ollama 重排模型: {self.model_name}")
                self._ollama_reranker = OllamaReranker(self.model_name)
                logger.info("Ollama 重排模型加载完成")
            except Exception as e:
                logger.warning(f"Ollama 重排模型加载失败: {e}，将使用嵌入兜底")
                self._ollama_reranker = None
            return

        if self._model is None:
            try:
                from sentence_transformers import CrossEncoder

                logger.info(f"正在加载重排模型: {self.model_name}")
                # D 显存防抖：显式绑定 CPU，避免 CrossEncoder 抢占 qwen3:4b 显存
                self._model = CrossEncoder(self.model_name, device="cpu")
                logger.info("重排模型加载完成")
            except ImportError:
                logger.warning("sentence-transformers 未安装，重排将使用嵌入兜底")
                self._model = None
            except Exception as e:
                logger.warning(f"重排模型加载失败: {e}，将使用嵌入兜底")
                self._model = None

    async def _load_model_async(self):
        """异步加载重排模型。

        B3：模型加载为首次调用的 CPU/IO 密集操作，移入线程池避免阻塞事件循环；
        用锁保证并发请求下仅加载一次（double-checked）。
        """
        if (self.provider == "ollama" and self._ollama_reranker is not None) or (
            self.provider != "ollama" and self._model is not None
        ):
            return

        def _guarded():
            with self._model_load_lock:
                # double-checked：并发下仅首个线程真正加载
                if (self.provider == "ollama" and self._ollama_reranker is not None) or (
                    self.provider != "ollama" and self._model is not None
                ):
                    return
                self._load_model()

        await asyncio.to_thread(_guarded)

    def _load_embeddings_fallback(self):
        """加载 OllamaEmbeddings 作为 rerank 兜底（增强4：嵌入兜底）"""
        try:
            current_key = model_manager.embedding_model_key
        except Exception:
            current_key = ""
        if self._fallback_loaded and current_key == getattr(self, "_embeddings_key", ""):
            return
        self._fallback_loaded = True
        try:
            # B2：走 model_manager 共享 OllamaEmbeddings 单例
            self._embeddings = model_manager.get_embeddings()
            self._embeddings_key = current_key
            logger.info("rerank 嵌入兜底已加载")
        except Exception as e:
            logger.warning(f"rerank 嵌入兜底加载失败: {e}")
            self._embeddings = None

    async def _rerank_by_embeddings(
        self, query: str, contents: List[WebContent], top_k: int
    ) -> List[WebContent]:
        """用 OllamaEmbeddings 计算余弦相似度重排（增强4：嵌入兜底）"""
        self._load_embeddings_fallback()
        if self._embeddings is None:
            return contents[:top_k]
        try:
            import numpy as np
            query_vec = await self._embeddings.aembed_query(query)
            doc_vecs = await self._embeddings.aembed_documents([c.content for c in contents])
            query_arr = np.array(query_vec)
            query_norm = np.linalg.norm(query_arr) + 1e-8
            for content, doc_vec in zip(contents, doc_vecs):
                doc_arr = np.array(doc_vec)
                doc_norm = np.linalg.norm(doc_arr) + 1e-8
                content.score = float(query_arr @ doc_arr / (query_norm * doc_norm))
            ranked = sorted(contents, key=lambda x: x.score, reverse=True)
            return ranked[:top_k]
        except Exception as e:
            logger.warning(f"嵌入兜底重排失败: {e}")
            return contents[:top_k]

    async def rerank(self, query: str, contents: List[WebContent], top_k: int = 5) -> List[WebContent]:
        if not contents:
            return []

        # C1：重排关闭或未配置模型时按原文顺序截断
        if not settings.search.SEARCH_ENABLE_RERANK or not self.model_name:
            return contents[:top_k]

        await self._load_model_async()
        # C1：与知识库 rerank 对齐——先截断候选再打分，减少打分次数
        rerank_n = max(top_k, settings.search.SEARCH_RERANK_TOP_K)
        candidates = contents[:rerank_n]
        # Ollama 或 Cross-Encoder 可用：优先使用
        try:
            pairs = [(query, c.content) for c in candidates]
            if self.provider == "ollama" and self._ollama_reranker is not None:
                scores = await self._ollama_reranker.predict(pairs)
            elif self._model is not None:
                scores = await asyncio.to_thread(self._model.predict, pairs)
            else:
                raise RuntimeError("无可用重排序模型")

            for content, score in zip(candidates, scores):
                content.score = float(score)

            ranked = sorted(candidates, key=lambda x: x.score, reverse=True)
            return ranked[:top_k]
        except Exception as e:
            logger.warning(f"语义重排失败: {e}，降级为嵌入兜底")

        # 增强4：主重排模型不可用时，用 OllamaEmbeddings 嵌入兜底
        return await self._rerank_by_embeddings(query, candidates, top_k)
