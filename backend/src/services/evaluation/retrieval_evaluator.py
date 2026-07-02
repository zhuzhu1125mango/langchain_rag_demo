"""检索评估器。

计算检索质量的常用指标：命中率（Hit Rate）、平均倒数排名（MRR）、
上下文精确率（Context Precision）与上下文召回率（Context Recall）。
同时支持无标注场景下基于 embedding 相似度的质量估计。
"""

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from src.config import settings

logger = logging.getLogger(__name__)


@dataclass
class RetrievalEvalResult:
    """检索评估结果。"""

    hit_rate: float = 0.0
    mrr: float = 0.0
    context_precision: float = 0.0
    context_recall: float = 0.0
    avg_similarity: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hit_rate": self.hit_rate,
            "mrr": self.mrr,
            "context_precision": self.context_precision,
            "context_recall": self.context_recall,
            "avg_similarity": self.avg_similarity,
            "details": self.details,
        }


class RetrievalEvaluator:
    """检索质量评估器。

    支持两种评估模式：
    1. 有标注评估：传入期望文档 ID 或内容，计算 Hit Rate / MRR / Precision / Recall。
    2. 无标注评估：基于 question 与 retrieved_docs 的 embedding 相似度估计检索质量。
    """

    def __init__(
        self,
        embeddings: Optional[Any] = None,
        similarity_threshold: Optional[float] = None,
    ):
        self.embeddings = embeddings
        self.similarity_threshold = similarity_threshold or 0.6
        self._embedding_model_name: Optional[str] = None

    def _get_embeddings(self) -> Optional[Any]:
        """延迟初始化 embedding 模型。"""
        current_model = settings.model.EMBEDDING_MODEL_NAME
        if self.embeddings is None or self._embedding_model_name != current_model:
            try:
                from langchain_ollama import OllamaEmbeddings

                self.embeddings = OllamaEmbeddings(model=current_model)
                self._embedding_model_name = current_model
            except Exception as e:
                logger.warning(f"检索评估器 embedding 模型初始化失败: {e}")
                self.embeddings = None
        return self.embeddings

    @staticmethod
    def _cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
        """计算两个向量的余弦相似度。"""
        if not vec_a or not vec_b or len(vec_a) != len(vec_b):
            return 0.0
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = math.sqrt(sum(a * a for a in vec_a))
        norm_b = math.sqrt(sum(b * b for b in vec_b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    @staticmethod
    def _doc_id(doc: Any) -> str:
        """从文档对象中提取稳定 ID。"""
        if isinstance(doc, dict):
            return str(doc.get("id", doc.get("document_id", "")))
        metadata = getattr(doc, "metadata", None) or {}
        return str(metadata.get("id", metadata.get("document_id", "")))

    @staticmethod
    def _doc_content(doc: Any) -> str:
        """从文档对象中提取文本内容。"""
        if isinstance(doc, dict):
            return doc.get("page_content", doc.get("content", ""))
        return getattr(doc, "page_content", str(doc))

    async def _embed_texts(self, texts: Sequence[str]) -> List[List[float]]:
        """异步编码文本列表。"""
        embeddings = self._get_embeddings()
        if embeddings is None or not texts:
            return []
        try:
            return await embeddings.aembed_documents(list(texts))
        except Exception as e:
            logger.warning(f"检索评估 embedding 计算失败: {e}")
            return []

    async def evaluate(
        self,
        question: str,
        retrieved_docs: Sequence[Any],
        expected_doc_ids: Optional[Sequence[str]] = None,
        expected_contents: Optional[Sequence[str]] = None,
    ) -> RetrievalEvalResult:
        """评估检索结果质量。

        Args:
            question: 用户问题。
            retrieved_docs: 检索到的文档列表。
            expected_doc_ids: 期望命中的文档 ID 列表（可选）。
            expected_contents: 期望命中的文档内容列表（可选）。

        Returns:
            RetrievalEvalResult: 评估结果。
        """
        retrieved_docs = list(retrieved_docs or [])
        if not retrieved_docs:
            return RetrievalEvalResult(
                details={"reason": "检索结果为空"},
            )

        # 基于 ID 的相关性判定
        expected_id_set = set(expected_doc_ids or [])
        relevance_by_id = [self._doc_id(d) in expected_id_set for d in retrieved_docs]

        # 基于内容子串的相关性判定（当未提供 ID 时作为补充）
        expected_content_set = set(expected_contents or [])
        relevance_by_content = [
            any(exp in self._doc_content(d) for exp in expected_content_set)
            for d in retrieved_docs
        ]

        # 合并两种相关性判定
        relevance = [
            rel_id or rel_content
            for rel_id, rel_content in zip(relevance_by_id, relevance_by_content)
        ]

        result = RetrievalEvalResult()

        if any(relevance):
            result.hit_rate = 1.0
            first_rank = relevance.index(True) + 1
            result.mrr = 1.0 / first_rank

        result.context_precision = sum(relevance) / len(relevance) if relevance else 0.0

        total_expected = len(expected_id_set) + len(expected_content_set)
        if total_expected > 0:
            result.context_recall = sum(relevance) / total_expected

        # 无标注或需要补充时，计算 question 与文档的 avg_similarity
        result.avg_similarity = await self._evaluate_similarity(question, retrieved_docs)

        result.details = {
            "retrieved_count": len(retrieved_docs),
            "expected_count": total_expected,
            "relevance": relevance,
        }
        return result

    async def _evaluate_similarity(
        self,
        question: str,
        retrieved_docs: Sequence[Any],
    ) -> float:
        """计算问题与检索文档的平均 embedding 相似度。"""
        embeddings = self._get_embeddings()
        if embeddings is None or not question or not retrieved_docs:
            return 0.0

        try:
            query_embedding = await embeddings.aembed_query(question)
            doc_texts = [self._doc_content(d) for d in retrieved_docs]
            doc_embeddings = await self._embed_texts(doc_texts)
            if not doc_embeddings:
                return 0.0

            similarities = [
                self._cosine_similarity(query_embedding, emb)
                for emb in doc_embeddings
            ]
            return sum(similarities) / len(similarities)
        except Exception as e:
            logger.warning(f"检索相似度评估失败: {e}")
            return 0.0

    async def evaluate_batch(
        self,
        samples: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """批量评估多个查询的检索结果。

        Args:
            samples: 每个样本包含 question、retrieved_docs、expected_doc_ids（可选）。

        Returns:
            聚合评估报告。
        """
        results = []
        for sample in samples:
            result = await self.evaluate(
                question=sample.get("question", ""),
                retrieved_docs=sample.get("retrieved_docs", []),
                expected_doc_ids=sample.get("expected_doc_ids"),
                expected_contents=sample.get("expected_contents"),
            )
            results.append(result.to_dict())

        if not results:
            return {"samples": [], "aggregated": {}}

        aggregated = {
            "hit_rate": sum(r["hit_rate"] for r in results) / len(results),
            "mrr": sum(r["mrr"] for r in results) / len(results),
            "context_precision": sum(r["context_precision"] for r in results) / len(results),
            "context_recall": sum(r["context_recall"] for r in results) / len(results),
            "avg_similarity": sum(r["avg_similarity"] for r in results) / len(results),
        }
        return {"samples": results, "aggregated": aggregated}
