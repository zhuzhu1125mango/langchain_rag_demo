"""知识库推荐服务。

基于用户问题从向量库中检索相关文档，并按知识库聚合相关性分数进行推荐。
"""

import logging

logger = logging.getLogger("rag_system")


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

            recommendations = []
            for kb_id, scores in kb_scores.items():
                avg_score = sum(scores) / len(scores)
                recommendations.append({
                    'kb_id': kb_id,
                    'relevance_score': avg_score,
                    'matched_chunks': len(scores)
                })

            recommendations.sort(key=lambda x: x['relevance_score'], reverse=True)

            return recommendations[:top_k]
        except Exception as e:
            logger.error(f"知识库推荐失败: {str(e)}", exc_info=True)
            return []
