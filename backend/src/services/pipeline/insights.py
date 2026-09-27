"""知识服务委托方法（自 rag_chain.py 拆出的 Mixin）。

包含改写、分类、对比、推荐、查重、分析、图谱等对独立服务模块的
薄委托封装（保持 RAGChain 对外 API 不变）。
"""


class InsightsMixin:
    """知识服务委托方法集（由 RAGChain 组装）。"""

    async def rewrite_question(self, question: str) -> dict:
        """
        重写问题，使其更加清晰、完整

        委托给 QuestionProcessor 处理。

        Args:
            question: 原始问题

        Returns:
            dict: {
                "rewritten": 重写后的问题,
                "original": 原始问题,
                "changes": 改动说明
            }
        """
        return await self.question_processor.rewrite_question(question)

    async def classify_question(self, question: str) -> dict:
        """
        识别问题类型

        委托给 QuestionProcessor 处理。

        Args:
            question: 用户问题

        Returns:
            dict: {
                "type": 问题类型,
                "subtype": 子类型,
                "confidence": 置信度,
                "description": 类型说明
            }
        """
        return await self.question_processor.classify_question(question)

    async def compare_knowledge_bases(self, question: str, kb_ids: list, kb_name_map: dict = None) -> dict:
        """
        对比多个知识库的答案差异

        委托给 KBComparator 处理。

        Args:
            question: 用户问题
            kb_ids: 知识库ID列表
            kb_name_map: 知识库名称映射（可选）

        Returns:
            dict: 各知识库的答案对比，包含差异分析
        """
        return await self.kb_comparator.compare_knowledge_bases(question, kb_ids, kb_name_map)

    async def generate_suggestions(self, question=None, history_context="", kb_ids=None):
        """
        根据上下文生成智能推荐问题

        委托给 QuestionProcessor 处理。

        Args:
            question: 当前问题（可选）
            history_context: 历史对话上下文（可选）
            kb_ids: 指定的知识库ID列表（可选）

        Returns:
            list: 推荐问题列表（字符串数组）
        """
        return await self.question_processor.generate_suggestions(question, history_context, kb_ids)

    async def recommend_knowledge_bases(self, question: str, top_k: int = 3, kb_ids: list = None) -> list:
        """
        基于问题自动推荐相关知识库

        委托给 KBRecommender 处理。

        Args:
            question: 用户问题
            top_k: 返回的知识库数量
            kb_ids: 候选知识库ID列表（调用方须限定为当前用户拥有的 KB；
                传 None 会检索全部用户的知识库）

        Returns:
            list: 推荐的知识库列表，按相关性排序
        """
        return await self.kb_recommender.recommend_knowledge_bases(
            question, top_k, kb_ids=kb_ids
        )

    async def detect_duplicates(self, content: str, kb_id: str = None, threshold: float = 0.85) -> list:
        """
        检测重复或相似文档

        委托给 DocumentAnalyzer 处理。

        Args:
            content: 待检测的文档内容
            kb_id: 限定知识库ID（可选）
            threshold: 相似度阈值（0-1）

        Returns:
            list: 相似文档列表
        """
        return await self.document_analyzer.detect_duplicates(content, kb_id, threshold)

    async def analyze_document(self, content: str) -> dict:
        """
        合并文档智能分析（分类 + 质量评估）为一次 LLM 调用。

        委托给 DocumentAnalyzer 处理。

        Args:
            content: 文档内容

        Returns:
            dict: 分类 + 质量评估合并结果
        """
        return await self.document_analyzer.analyze_document(content)

    async def generate_knowledge_graph(self, kb_ids: list = None) -> dict:
        """
        生成知识库关系图谱

        委托给 KnowledgeGraphGenerator 处理。

        Args:
            kb_ids: 知识库ID列表（可选）

        Returns:
            dict: 知识图谱数据
        """
        return await self.knowledge_graph_generator.generate_knowledge_graph(kb_ids)
