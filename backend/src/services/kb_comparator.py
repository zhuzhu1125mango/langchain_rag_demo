"""知识库对比服务。

对比多个知识库对同一问题的回答差异，包括分别检索生成答案、相似度分析与关键差异提取。
"""

import asyncio
import logging
from typing import List

logger = logging.getLogger("rag_system")


class KBComparator:
    """
    知识库对比器

    对多个知识库的答案进行对比分析，包括相似度计算和差异提取。
    """

    def __init__(self, llm, rag_chain):
        """
        初始化知识库对比器

        Args:
            llm: LLM 实例
            rag_chain: RAGChain 实例，用于复用核心检索和相似度能力
        """
        self.llm = llm
        self.rag_chain = rag_chain

    async def compare_knowledge_bases(self, question: str, kb_ids: List[str], kb_name_map: dict = None) -> dict:
        """
        对比多个知识库的答案差异

        Args:
            question: 用户问题
            kb_ids: 知识库ID列表
            kb_name_map: 知识库名称映射（可选）

        Returns:
            dict: 各知识库的答案对比，包含差异分析
        """
        results = {}
        all_answers = []

        for kb_id in kb_ids:
            try:
                # 从指定知识库检索
                docs = await self.rag_chain._retrieve_documents(question, kb_ids=[kb_id])
                source_texts, source_metadata = self.rag_chain._extract_source_info(docs)

                if docs:
                    context = "\n\n".join([doc.page_content for doc in docs])
                    template = """
你是一个智能助手，请根据以下知识库的信息回答用户的问题。

参考信息:
{context}

当前问题:
{question}

请给出准确的回答。
"""
                    prompt = template.format(context=context, question=question)
                    answer = await self.llm.ainvoke(prompt)
                    results[kb_id] = {
                        "answer": answer.content,
                        "sources": source_texts,
                        "source_metadata": source_metadata,
                        "has_answer": True,
                        "doc_count": len(docs)
                    }
                    all_answers.append({
                        "kb_id": kb_id,
                        "answer": answer.content
                    })
                else:
                    results[kb_id] = {
                        "answer": "该知识库中没有找到相关信息",
                        "sources": [],
                        "source_metadata": [],
                        "has_answer": False,
                        "doc_count": 0
                    }
            except Exception as e:
                logger.error(f"从知识库 {kb_id} 检索失败: {str(e)}", exc_info=True)
                results[kb_id] = {
                    "answer": f"检索失败: {str(e)}",
                    "sources": [],
                    "source_metadata": [],
                    "has_answer": False,
                    "error": str(e),
                    "doc_count": 0
                }

        # 添加差异分析
        analysis = await self._analyze_answer_differences(all_answers, kb_ids, kb_name_map)
        results["_analysis"] = analysis

        return results

    async def _analyze_answer_differences(self, answers: list, kb_ids: List[str], kb_name_map: dict = None) -> dict:
        """
        分析多个知识库答案之间的差异

        Args:
            answers: 答案列表
            kb_ids: 知识库ID列表
            kb_name_map: 知识库名称映射（可选）

        Returns:
            dict: 差异分析结果
        """
        if len(answers) < 2:
            return {
                "similarity_score": 1.0,
                "consistency": "high",
                "summary": "只有一个知识库返回了答案",
                "differences": []
            }

        # 初始化相似度模型
        self.rag_chain._init_similarity_model()

        if self.rag_chain.sentence_transformer is None:
            return {
                "similarity_score": 0.5,
                "consistency": "medium",
                "summary": "无法计算相似度",
                "differences": []
            }

        try:
            # 计算所有答案对之间的相似度
            similarities = []
            comparisons = []

            for i in range(len(answers)):
                for j in range(i + 1, len(answers)):
                    emb1 = await asyncio.to_thread(self.rag_chain.sentence_transformer.encode, answers[i]["answer"])
                    emb2 = await asyncio.to_thread(self.rag_chain.sentence_transformer.encode, answers[j]["answer"])
                    similarity = float(self.rag_chain.similarity_util.cos_sim(emb1, emb2))
                    similarities.append(similarity)

                    kb1_name = self._get_kb_display_name(answers[i]["kb_id"], kb_name_map)
                    kb2_name = self._get_kb_display_name(answers[j]["kb_id"], kb_name_map)

                    comparisons.append({
                        "kb1": answers[i]["kb_id"],
                        "kb1_name": kb1_name,
                        "kb2": answers[j]["kb_id"],
                        "kb2_name": kb2_name,
                        "similarity": similarity
                    })

            avg_similarity = sum(similarities) / len(similarities) if similarities else 0.5

            # 判断一致性等级
            if avg_similarity >= 0.8:
                consistency = "high"
                summary = "各知识库答案高度一致"
            elif avg_similarity >= 0.5:
                consistency = "medium"
                summary = "各知识库答案存在一定差异"
            else:
                consistency = "low"
                summary = "各知识库答案差异较大"

            # 提取关键差异点
            differences = await self._extract_key_differences(answers, kb_ids, kb_name_map)

            return {
                "similarity_score": round(avg_similarity, 2),
                "consistency": consistency,
                "summary": summary,
                "differences": differences,
                "comparisons": comparisons
            }
        except Exception as e:
            logger.error(f"答案差异分析失败: {str(e)}", exc_info=True)
            return {
                "similarity_score": 0.5,
                "consistency": "unknown",
                "summary": f"分析失败: {str(e)}",
                "differences": []
            }

    def _get_kb_display_name(self, kb_id: str, kb_name_map: dict = None) -> str:
        """获取知识库的显示名称"""
        if kb_name_map and kb_id in kb_name_map:
            return kb_name_map[kb_id]
        return kb_id[:8]

    async def _extract_key_differences(self, answers: list, kb_ids: List[str], kb_name_map: dict = None) -> list:
        """
        提取答案之间的关键差异点

        Args:
            answers: 答案列表
            kb_ids: 知识库ID列表
            kb_name_map: 知识库名称映射（可选）

        Returns:
            list: 差异点列表
        """
        template = """
你是一个答案分析专家，请分析以下答案的差异点。

{answers_text}

请分析并列出这些答案的主要差异点，格式如下：
1. [差异点标题]: [详细说明涉及的答案]

请列出3-5个最重要的差异点。
"""

        try:
            answers_text = "\n\n".join([
                f"答案 {i+1} (来自知识库 {self._get_kb_display_name(a['kb_id'], kb_name_map)}):\n{a['answer'][:500]}"
                for i, a in enumerate(answers)
            ])

            prompt = template.format(answers_text=answers_text)
            response = await self.llm.ainvoke(prompt)

            # 简单解析差异点
            lines = response.content.strip().split('\n')
            differences = []
            for line in lines:
                line = line.strip()
                if line and (line[0].isdigit() or line.startswith('-')):
                    # 移除编号
                    diff = line.lstrip('0123456789.- ')
                    if diff:
                        differences.append(diff)

            return differences[:5]
        except Exception as e:
            logger.error(f"提取关键差异失败: {str(e)}", exc_info=True)
            return []
