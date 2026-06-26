"""知识图谱生成服务。

基于知识库文档生成知识图谱节点与边，包括知识库-文档包含关系与文档间相似关系。
"""

import asyncio
import logging

logger = logging.getLogger("rag_system")


class KnowledgeGraphGenerator:
    """
    知识图谱生成器

    根据知识库和文档数据生成节点和边的关系图谱。
    """

    def __init__(self, llm, rag_chain):
        """
        初始化知识图谱生成器

        Args:
            llm: LLM 实例
            rag_chain: RAGChain 实例，用于复用核心检索和相似度能力
        """
        self.llm = llm
        self.rag_chain = rag_chain

    async def generate_knowledge_graph(self, kb_ids: list = None) -> dict:
        """
        生成知识库关系图谱

        Args:
            kb_ids: 知识库ID列表（可选）

        Returns:
            dict: 知识图谱数据
        """
        if not self.rag_chain._has_vector_store():
            return {"nodes": [], "edges": [], "summary": "没有可用的向量库"}

        try:
            documents = []
            if kb_ids:
                for kb_id in kb_ids:
                    docs = await self.rag_chain._retrieve_documents("", kb_ids=[kb_id])
                    documents.extend(docs)
            else:
                documents = await self.rag_chain._retrieve_documents("")

            nodes = []
            edges = []
            kb_nodes = {}
            doc_nodes = {}

            for doc in documents:
                doc_id = doc.metadata.get('document_id', str(id(doc)))
                kb_id = doc.metadata.get('kb_id', '')
                filename = doc.metadata.get('filename', 'unknown')

                if kb_id and kb_id not in kb_nodes:
                    kb_nodes[kb_id] = {
                        "id": f"kb_{kb_id}",
                        "label": f"知识库_{kb_id[:8]}",
                        "type": "knowledge_base",
                        "size": 30
                    }

                doc_nodes[doc_id] = {
                    "id": f"doc_{doc_id}",
                    "label": filename,
                    "type": "document",
                    "size": 20,
                    "kb_id": kb_id
                }

            for kb_id, node in kb_nodes.items():
                nodes.append(node)

            for doc_id, node in doc_nodes.items():
                nodes.append(node)
                if node.get('kb_id'):
                    edges.append({
                        "from": f"kb_{node['kb_id']}",
                        "to": f"doc_{doc_id}",
                        "label": "包含"
                    })

            self.rag_chain._init_similarity_model()
            if self.rag_chain.sentence_transformer is not None:
                doc_list = list(doc_nodes.values())
                for i in range(len(doc_list)):
                    for j in range(i + 1, len(doc_list)):
                        doc1 = documents[i]
                        doc2 = documents[j]
                        content1 = doc1.page_content if hasattr(doc1, 'page_content') else str(doc1)
                        content2 = doc2.page_content if hasattr(doc2, 'page_content') else str(doc2)

                        try:
                            emb1 = await asyncio.to_thread(self.rag_chain.sentence_transformer.encode, content1)
                            emb2 = await asyncio.to_thread(self.rag_chain.sentence_transformer.encode, content2)
                            similarity = float(self.rag_chain.similarity_util.cos_sim(emb1, emb2))

                            if similarity > 0.7:
                                edges.append({
                                    "from": doc_list[i]['id'],
                                    "to": doc_list[j]['id'],
                                    "label": f"相似({similarity:.2f})",
                                    "weight": similarity
                                })
                        except:
                            continue

            return {
                "nodes": nodes,
                "edges": edges,
                "summary": f"共{len(nodes)}个节点，{len(edges)}条关系"
            }
        except Exception as e:
            logger.error(f"生成知识图谱失败: {str(e)}", exc_info=True)
            return {"nodes": [], "edges": [], "summary": f"生成失败: {str(e)}"}
