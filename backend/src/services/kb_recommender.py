"""知识库推荐服务。

基于用户问题从向量库中检索相关文档，并按知识库聚合相关性分数进行推荐。
P3 增强：对有 Wiki 索引页的 KB 融合「覆盖先验」（索引页 embedding 与问题余弦），
缓解主题命中但 chunk 相似度整体偏低导致的推荐盲区（§11.1）。
"""

import logging

from src.config import settings

logger = logging.getLogger("rag_system")


def fuse_prior(chunk_avg: float, index_sim, weight: float) -> float:
    """融合公式：final = chunk_avg × (1−w) + index_sim × w。

    index_sim 为 None（KB 无 index 页/先验未算出）时保持纯 chunk 分。
    """
    if index_sim is None or weight <= 0:
        return chunk_avg
    return chunk_avg * (1 - weight) + index_sim * weight


class KBRecommender:
    """
    知识库推荐器

    根据用户问题从向量库中检索相关文档，并按知识库聚合相关性分数。
    """

    def __init__(self, rag_chain):
        """
        初始化知识库推荐器

        Args:
            rag_chain: RAGChain 实例，用于复用核心检索能力
        """
        self.rag_chain = rag_chain

    async def recommend_knowledge_bases(self, question: str, top_k: int = 3) -> list:
        """
        基于问题自动推荐相关知识库

        Args:
            question: 用户问题
            top_k: 返回的知识库数量

        Returns:
            list: 推荐的知识库列表，按相关性排序
        """
        if not self.rag_chain._has_vector_store():
            return []

        try:
            docs = await self.rag_chain._retrieve_documents(question, kb_ids=None)

            if not docs:
                return []

            kb_scores = {}
            for doc in docs:
                kb_id = doc.metadata.get('kb_id', '')
                score = doc.metadata.get('score', 0.0)

                if kb_id:
                    if kb_id not in kb_scores:
                        kb_scores[kb_id] = []
                    kb_scores[kb_id].append(score)

            priors = await self._get_route_priors(question, list(kb_scores.keys()))
            weight = settings.wiki_compile.WIKI_ROUTE_PRIOR_WEIGHT

            recommendations = []
            for kb_id, scores in kb_scores.items():
                avg_score = sum(scores) / len(scores)
                final_score = fuse_prior(avg_score, priors.get(kb_id), weight)
                recommendations.append({
                    'kb_id': kb_id,
                    'relevance_score': final_score,
                    'chunk_score': avg_score,
                    'route_prior': priors.get(kb_id),
                    'matched_chunks': len(scores)
                })

            recommendations.sort(key=lambda x: x['relevance_score'], reverse=True)

            return recommendations[:top_k]
        except Exception as e:
            logger.error(f"知识库推荐失败: {str(e)}", exc_info=True)
            return []

    async def _get_route_priors(self, question: str, kb_ids: list) -> dict:
        """取覆盖先验分：权重为 0 时跳过；失败由 WikiRoutePrior 兜底返回空 dict
        （空 dict → 所有 KB 保持纯 chunk 分，行为与现状一致）。"""
        weight = settings.wiki_compile.WIKI_ROUTE_PRIOR_WEIGHT
        if weight <= 0 or not kb_ids:
            return {}
        from src.services.wiki_route_prior import wiki_route_prior

        return await wiki_route_prior.get_priors(question, kb_ids)
