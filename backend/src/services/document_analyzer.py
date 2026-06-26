"""
文档分析服务

负责文档相关的智能分析，包括：
1. 重复/相似文档检测
2. 文档质量评估
3. 文档自动分类
"""

import asyncio
import json
import logging
import re

logger = logging.getLogger("rag_system")


class DocumentAnalyzer:
    """
    文档分析器

    对文档进行重复检测、质量评估和自动分类。
    """

    def __init__(self, llm, rag_chain):
        """
        初始化文档分析器

        Args:
            llm: LLM 实例
            rag_chain: RAGChain 实例，用于复用核心检索和相似度能力
        """
        self.llm = llm
        self.rag_chain = rag_chain

    async def detect_duplicates(self, content: str, kb_id: str = None, threshold: float = 0.85) -> list:
        """
        检测重复或相似文档

        Args:
            content: 待检测的文档内容
            kb_id: 限定知识库ID（可选）
            threshold: 相似度阈值（0-1）

        Returns:
            list: 相似文档列表
        """
        if not self.rag_chain._has_vector_store():
            return []

        self.rag_chain._init_similarity_model()
        if self.rag_chain.sentence_transformer is None:
            return []

        try:
            docs = await self.rag_chain._retrieve_documents(content, kb_ids=[kb_id] if kb_id else None)

            if not docs:
                return []

            content_embedding = await asyncio.to_thread(self.rag_chain.sentence_transformer.encode, content)
            duplicates = []

            for doc in docs:
                doc_content = doc.page_content if hasattr(doc, 'page_content') else str(doc)
                doc_embedding = await asyncio.to_thread(self.rag_chain.sentence_transformer.encode, doc_content)
                similarity = float(self.rag_chain.similarity_util.cos_sim(content_embedding, doc_embedding))

                if similarity >= threshold:
                    duplicates.append({
                        'document_id': doc.metadata.get('document_id', ''),
                        'filename': doc.metadata.get('filename', 'unknown'),
                        'similarity': similarity,
                        'kb_id': doc.metadata.get('kb_id', '')
                    })

            duplicates.sort(key=lambda x: x['similarity'], reverse=True)
            return duplicates
        except Exception as e:
            logger.error(f"重复检测失败: {str(e)}", exc_info=True)
            return []

    async def evaluate_document_quality(self, content: str) -> dict:
        """
        评估文档质量和完整性

        Args:
            content: 文档内容

        Returns:
            dict: 质量评估结果
        """
        template = """
你是一个文档质量评估专家，请根据以下标准评估文档质量：

文档内容:
{content}

请按照以下标准评估：
1. 完整性(0-100): 内容是否完整覆盖主题
2. 准确性(0-100): 信息是否准确可靠
3. 结构清晰度(0-100): 组织结构是否清晰
4. 语言质量(0-100): 语言表达是否规范
5. 相关性(0-100): 内容是否与常见知识库主题相关

请以JSON格式返回评估结果：
{{
    "overall_score": 综合评分,
    "completeness": 完整性评分,
    "accuracy": 准确性评分,
    "structure": 结构清晰度评分,
    "language_quality": 语言质量评分,
    "relevance": 相关性评分,
    "summary": "简短评估总结",
    "suggestions": ["改进建议1", "改进建议2"]
}}

请直接返回JSON，不要添加其他内容。
"""
        prompt = template.format(content=content[:2000])

        try:
            response = await self.llm.ainvoke(prompt)

            content = response.content.strip()
            json_match = re.search(r'\{[\s\S]*\}', content)
            if json_match:
                result = json.loads(json_match.group())
                return result
            else:
                return {
                    "overall_score": 50,
                    "completeness": 50,
                    "accuracy": 50,
                    "structure": 50,
                    "language_quality": 50,
                    "relevance": 50,
                    "summary": "无法解析评估结果",
                    "suggestions": ["建议重新评估"]
                }
        except Exception as e:
            logger.error(f"文档质量评估失败: {str(e)}", exc_info=True)
            return {
                "overall_score": 50,
                "completeness": 50,
                "accuracy": 50,
                "structure": 50,
                "language_quality": 50,
                "relevance": 50,
                "summary": "评估失败",
                "suggestions": ["建议重新评估"]
            }

    async def classify_document(self, content: str) -> dict:
        """
        自动识别文档类型和主题

        Args:
            content: 文档内容

        Returns:
            dict: 分类结果
        """
        template = """
你是一个文档分类专家，请分析以下文档并给出分类结果：

文档内容:
{content}

请按照以下格式分类：
1. 文档类型: 技术文档/产品文档/报告/论文/手册/指南/其他
2. 主题标签: 最多5个关键词
3. 适用领域: 描述适用的业务领域
4. 内容摘要: 简短摘要

请以JSON格式返回：
{{
    "document_type": "文档类型",
    "topics": ["标签1", "标签2", ...],
    "domain": "适用领域",
    "summary": "内容摘要"
}}

请直接返回JSON，不要添加其他内容。
"""
        prompt = template.format(content=content[:2000])

        try:
            response = await self.llm.ainvoke(prompt)

            content = response.content.strip()
            json_match = re.search(r'\{[\s\S]*\}', content)
            if json_match:
                result = json.loads(json_match.group())
                return result
            else:
                return {
                    "document_type": "其他",
                    "topics": ["未分类"],
                    "domain": "未知",
                    "summary": "无法解析文档内容"
                }
        except Exception as e:
            logger.error(f"文档分类失败: {str(e)}", exc_info=True)
            return {
                "document_type": "其他",
                "topics": ["未分类"],
                "domain": "未知",
                "summary": "分类失败"
            }

    @staticmethod
    def analyze_document_content(content: str) -> tuple:
        """
        分析文档内容，进行自动分类（基于关键词规则）

        Args:
            content: 文档内容

        Returns:
            tuple: (document_type, topics, domain)
        """
        content_lower = content.lower()

        document_type = "other"
        domain = "other"
        topics = []

        if any(keyword in content_lower for keyword in ["代码", "编程", "开发", "api", "function", "class", "method", "algorithm", "软件", "系统"]):
            document_type = "technical"
            domain = "it"
            topics.append("技术开发")
        elif any(keyword in content_lower for keyword in ["财务", "报表", "预算", "投资", "利润", "收入", "成本", "审计"]):
            document_type = "business"
            domain = "finance"
            topics.append("财务管理")
        elif any(keyword in content_lower for keyword in ["报告", "分析", "总结", "评估", "调研", "数据"]):
            document_type = "report"
            topics.append("数据分析")
        elif any(keyword in content_lower for keyword in ["手册", "指南", "教程", "说明", "使用", "操作"]):
            document_type = "manual"
            topics.append("使用指南")
        elif any(keyword in content_lower for keyword in ["政策", "规定", "通知", "公告", "条例", "办法"]):
            document_type = "policy"
            domain = "government"
            topics.append("政策法规")
        elif any(keyword in content_lower for keyword in ["新闻", "消息", "报道", "发布", "动态"]):
            document_type = "news"
            topics.append("新闻资讯")

        if any(keyword in content_lower for keyword in ["医疗", "健康", "医院", "药品", "疾病", "诊断"]):
            domain = "healthcare"
            topics.append("医疗健康")
        elif any(keyword in content_lower for keyword in ["教育", "学校", "学习", "培训", "课程", "学生"]):
            domain = "education"
            topics.append("教育培训")
        elif any(keyword in content_lower for keyword in ["法律", "法规", "合同", "诉讼", "权利", "义务"]):
            domain = "legal"
            topics.append("法律合规")
        elif any(keyword in content_lower for keyword in ["管理", "企业", "公司", "组织", "战略", "运营"]):
            domain = "enterprise"
            topics.append("企业管理")

        if "安全" in content_lower:
            topics.append("安全")
        if "数据" in content_lower:
            topics.append("数据分析")
        if "系统" in content_lower:
            topics.append("系统设计")
        if "流程" in content_lower:
            topics.append("流程管理")

        if not topics:
            topics = ["综合内容"]

        return document_type, topics[:5], domain

    @staticmethod
    def evaluate_quality(content: str) -> dict:
        """
        评估文档质量（基于规则）

        Args:
            content: 文档内容

        Returns:
            dict: 质量评估结果
        """
        content_length = len(content)
        sentences = [s.strip() for s in content.split('。') if s.strip()]
        paragraphs = [p.strip() for p in content.split('\n\n') if p.strip()]

        completeness = min(100, content_length / 500 * 20)
        if content_length < 100:
            completeness = 20
            completeness_comment = "内容过于简短"
        elif content_length < 500:
            completeness = 50
            completeness_comment = "内容较为简短"
        elif content_length < 2000:
            completeness = 75
            completeness_comment = "内容基本完整"
        else:
            completeness = 90
            completeness_comment = "内容丰富完整"

        avg_sentence_length = sum(len(s) for s in sentences) / len(sentences) if sentences else 0
        if avg_sentence_length < 10:
            readability = 40
            readability_comment = "句子过短，可能影响可读性"
        elif avg_sentence_length > 100:
            readability = 50
            readability_comment = "句子过长，建议拆分"
        else:
            readability = 85
            readability_comment = "句子长度适中，可读性良好"

        structure_score = len(paragraphs) * 5
        if len(paragraphs) < 3:
            structure = 40
            structure_comment = "段落结构较少，建议增加分段"
        elif len(paragraphs) < 5:
            structure = 60
            structure_comment = "段落结构一般"
        else:
            structure = min(80, structure_score)
            structure_comment = "段落结构合理"

        has_headings = any(content.count(h) > 0 for h in ['\n# ', '\n## ', '\n### ', '一、', '二、', '1.', '2.'])
        if has_headings:
            structure += 10
            structure_comment = "段落结构合理，包含标题层级"

        relevance = 80
        relevance_comment = "文档内容相关性良好"

        scores = [completeness, readability, structure, relevance]
        overall_score = sum(scores) / len(scores)

        if overall_score >= 90:
            overall_grade = "优秀"
        elif overall_score >= 80:
            overall_grade = "良好"
        elif overall_score >= 70:
            overall_grade = "中等"
        elif overall_score >= 60:
            overall_grade = "及格"
        else:
            overall_grade = "需改进"

        suggestions = []
        if completeness < 60:
            suggestions.append("建议增加文档内容，使信息更加完整")
        if readability < 60:
            suggestions.append("建议优化句子结构，提高文档可读性")
        if structure < 60:
            suggestions.append("建议增加段落和标题，改善文档结构")
        if not suggestions:
            suggestions.append("文档质量良好，继续保持")

        return {
            "overall_score": round(overall_score, 1),
            "overall_grade": overall_grade,
            "completeness": round(completeness, 1),
            "completeness_comment": completeness_comment,
            "readability": round(readability, 1),
            "readability_comment": readability_comment,
            "structure": round(structure, 1),
            "structure_comment": structure_comment,
            "relevance": round(relevance, 1),
            "relevance_comment": relevance_comment,
            "suggestions": suggestions
        }
